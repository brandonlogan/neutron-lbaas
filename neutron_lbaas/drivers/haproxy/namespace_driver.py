# Copyright 2013 New Dream Network, LLC (DreamHost)
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import shutil
import socket

import netaddr
from neutron.agent.common import config
from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron.common import exceptions
from neutron.common import utils as n_utils
from neutron.i18n import _LE, _LW
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from oslo_config import cfg
from oslo_utils import excutils
from oslo_utils import importutils

from neutron_lbaas.agent import agent_device_driver
from neutron_lbaas.services.loadbalancer import constants as lb_const
from neutron_lbaas.services.loadbalancer.drivers.haproxy import jinja_cfg
from neutron_lbaas.services.loadbalancer.drivers.haproxy \
    import namespace_driver

LOG = logging.getLogger(__name__)
NS_PREFIX = 'qlbaas-'
DRIVER_NAME = 'test'

STATE_PATH_V2_APPEND = 'v2'

cfg.CONF.register_opts(namespace_driver.OPTS, 'haproxy')


def get_ns_name(namespace_id):
    return NS_PREFIX + namespace_id


class HaproxyNSDriver(agent_device_driver.AgentDeviceDriver):

    def __init__(self, conf, plugin_rpc):
        super(HaproxyNSDriver, self).__init__(conf, plugin_rpc)
        self.root_helper = config.get_root_helper(self.conf)
        self.state_path = conf.haproxy.loadbalancer_state_path
        self.state_path = os.path.join(
            self.conf.haproxy.loadbalancer_state_path, STATE_PATH_V2_APPEND)
        try:
            vif_driver = importutils.import_object(conf.interface_driver, conf)
        except ImportError:
            with excutils.save_and_reraise_exception():
                msg = (_('Error importing interface driver: %s')
                       % conf.interface_driver)
                LOG.error(msg)

        self.vif_driver = vif_driver

        self.load_balancer = LoadBalancerManager(self)
        self.listener = ListenerManager(self)
        self.pool = PoolManager(self)
        self.member = MemberManager(self)
        self.health_monitor = HealthMonitorManager(self)

    def get_name(self):
        return DRIVER_NAME

    def undeploy_instance(self, loadbalancer_id, **kwargs):
        pass

    def remove_orphans(self, known_loadbalancer_ids):
        pass

    @n_utils.synchronized('haproxy-driver')
    def deploy_instance(self, loadbalancer):
        """Deploys loadbalancer if necessary

        :return: True if loadbalancer was deployed, False otherwise
        """
        if not self.deployable(loadbalancer):
            return False

        if self.exists(loadbalancer.id):
            self.update(loadbalancer)
        else:
            self.create(loadbalancer)
        return True

    def update(self, loadbalancer):
        pid_path = self._get_state_file_path(loadbalancer.id, 'haproxy.pid')
        extra_args = ['-sf']
        extra_args.extend(p.strip() for p in open(pid_path, 'r'))
        self._spawn(loadbalancer, extra_args)

    def exists(self, loadbalancer):
        namespace = get_ns_name(loadbalancer.id)
        root_ns = ip_lib.IPWrapper(root_helper=self.root_helper)

        socket_path = self._get_state_file_path(
            loadbalancer.id, 'haproxy_stats.sock', False)
        if root_ns.netns.exists(namespace) and os.path.exists(socket_path):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(socket_path)
                return True
            except socket.error:
                pass
        return False

    def create(self, loadbalancer):
        namespace = get_ns_name(loadbalancer.id)

        self._plug(namespace, loadbalancer.vip_port)
        self._spawn(loadbalancer)

    def deployable(self, loadbalancer):
        """Returns True if loadbalancer is active and has active listeners."""
        if not loadbalancer:
            return False
        acceptable_listeners = [
            listener for listener in loadbalancer.listeners
            if (listener.provisioning_status != constants.PENDING_DELETE and
                listener.admin_state_up)]
        return (bool(acceptable_listeners) and loadbalancer.admin_state_up and
                loadbalancer.provisioning_status != constants.PENDING_DELETE)

    def _plug(self, namespace, port, reuse_existing=True):
        self.plugin_rpc.plug_vip_port(port.id)

        interface_name = self.vif_driver.get_device_name(port)

        if ip_lib.device_exists(interface_name,
                                root_helper=self.root_helper,
                                namespace=namespace):
            if not reuse_existing:
                raise exceptions.PreexistingDeviceFailure(
                    dev_name=interface_name
                )
        else:
            self.vif_driver.plug(
                port.network_id,
                port.id,
                interface_name,
                port.mac_address,
                namespace=namespace
            )

        # TODO(blogan): fixed_ips needs to have a subnet child object
        cidrs = [
            '%s/%s' % (ip.ip_address,
                       netaddr.IPNetwork(ip.subnet['cidr']).prefixlen)
            for ip in port.fixed_ips
        ]
        self.vif_driver.init_l3(interface_name, cidrs, namespace=namespace)

        gw_ip = port.fixed_ips[0].subnet.get('gateway_ip')

        if not gw_ip:
            host_routes = port.fixed_ips[0].subnet.get('host_routes', [])
            for host_route in host_routes:
                if host_route['destination'] == "0.0.0.0/0":
                    gw_ip = host_route['nexthop']
                    break

        if gw_ip:
            cmd = ['route', 'add', 'default', 'gw', gw_ip]
            ip_wrapper = ip_lib.IPWrapper(root_helper=self.root_helper,
                                          namespace=namespace)
            ip_wrapper.netns.execute(cmd, check_exit_code=False)
            # When delete and re-add the same vip, we need to
            # send gratuitous ARP to flush the ARP cache in the Router.
            gratuitous_arp = self.conf.haproxy.send_gratuitous_arp
            if gratuitous_arp > 0:
                for ip in port.fixed_ips:
                    cmd_arping = ['arping', '-U',
                                  '-I', interface_name,
                                  '-c', gratuitous_arp,
                                  ip.ip_address]
                    ip_wrapper.netns.execute(cmd_arping, check_exit_code=False)

    def _spawn(self, loadbalancer, extra_cmd_args=()):
        namespace = get_ns_name(loadbalancer.id)
        conf_path = self._get_state_file_path(loadbalancer.id, 'haproxy.conf')
        pid_path = self._get_state_file_path(loadbalancer.id,
                                             'haproxy.pid')
        sock_path = self._get_state_file_path(loadbalancer.id,
                                              'haproxy_stats.sock')
        user_group = self.conf.haproxy.user_group

        jinja_cfg.save_config(conf_path, loadbalancer, sock_path, user_group)
        cmd = ['haproxy', '-f', conf_path, '-p', pid_path]
        cmd.extend(extra_cmd_args)

        ns = ip_lib.IPWrapper(root_helper=self.root_helper,
                              namespace=namespace)
        ns.netns.execute(cmd)

        # remember deployed loadbalancer id
        self.deployed_loadbalancer_ids.add(loadbalancer.id)


class LoadBalancerManager(agent_device_driver.BaseLoadBalancerManager):

    def refresh(self, context, loadbalancer):
        loadbalancer = self.driver.plugin_rpc.get_logical_device(
            loadbalancer.id)
        if (not self.driver.deploy_instance(loadbalancer) and
                self.driver.exists(loadbalancer.id)):
            self.driver.undeploy_instance(loadbalancer.id)

    def delete(self, context, loadbalancer):
        if self.driver.exists(loadbalancer.id):
            self.driver.undeploy_instance(loadbalancer.id)

    def create(self, context, loadbalancer):
        # loadbalancer has no listeners then do nothing because haproxy will
        # not start without a tcp port.  Consider this successful anyway.
        if not loadbalancer.listeners:
            return
        self.refresh(context, loadbalancer)

    def stats(self, context, loadbalancer):
        return self.driver.get_stats(loadbalancer)

    def update(self, context, old_loadbalancer, loadbalancer):
        self.refresh(context, loadbalancer)


class ListenerManager(agent_device_driver.BaseListenerManager):

    def _remove_listener(self, loadbalancer, listener_id):
        index_to_remove = None
        for index, listener in enumerate(loadbalancer.listeners):
            if listener.id == listener_id:
                index_to_remove = index
        loadbalancer.listeners.pop(index_to_remove)

    def update(self, context, old_listener, new_listener):
        self.driver.load_balancer.refresh(context, new_listener.loadbalancer)

    def create(self, context, listener):
        self.driver.load_balancer.refresh(context, listener.loadbalancer)

    def delete(self, context, listener):
        loadbalancer = listener.loadbalancer
        self._remove_listener(loadbalancer, listener.id)
        if len(loadbalancer.listeners) > 0:
            self.driver.load_balancer.refresh(context, loadbalancer)
        else:
            # delete instance because haproxy will throw error if port is
            # missing in frontend
            self.driver.delete_instance(loadbalancer)


class PoolManager(agent_device_driver.BasePoolManager):

    def update(self, context, old_pool, new_pool):
        self.driver.load_balancer.refresh(context,
                                          new_pool.listener.loadbalancer)

    def create(self, context, pool):
        self.driver.load_balancer.refresh(context, pool.listener.loadbalancer)

    def delete(self, context, pool):
        loadbalancer = pool.listener.loadbalancer
        pool.listener.default_pool = None
        # just refresh because haproxy is fine if only frontend is listed
        self.driver.load_balancer.refresh(context, loadbalancer)


class MemberManager(agent_device_driver.BaseMemberManager):

    def _remove_member(self, pool, member_id):
        index_to_remove = None
        for index, member in enumerate(pool.members):
            if member.id == member_id:
                index_to_remove = index
        pool.members.pop(index_to_remove)

    def update(self, context, old_member, new_member):
        self.driver.load_balancer.refresh(
                context, new_member.pool.listener.loadbalancer)

    def create(self, context, member):
        self.driver.load_balancer.refresh(
            context, member.pool.listener.loadbalancer)

    def delete(self, context, member):
        self._remove_member(member.pool, member.id)
        self.driver.load_balancer.refresh(
            context, member.pool.listener.loadbalancer)


class HealthMonitorManager(agent_device_driver.BaseHealthMonitorManager):

    def update(self, context, old_hm, new_hm):
        self.driver.load_balancer.refresh(
            context, new_hm.pool.listener.loadbalancer)

    def create(self, context, hm):
        self.driver.load_balancer.refresh(
            context, hm.pool.listener.loadbalancer)

    def delete(self, context, hm):
        hm.pool.healthmonitor = None
        self.driver.load_balancer.refresh(context,
                                          hm.pool.listener.loadbalancer)
