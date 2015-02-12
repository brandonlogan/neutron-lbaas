# Copyright 2015 Rackspace.
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

import uuid

from neutron.common import exceptions as n_exc
from neutron.common import rpc as n_rpc
from neutron.db import agents_db
from neutron.extensions import portbindings
from neutron.i18n import _LW
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.services import provider_configuration as provconf
from oslo_config import cfg
import oslo_messaging as messaging
from oslo_utils import importutils

from neutron_lbaas.db.loadbalancer import loadbalancer_dbv2
from neutron_lbaas.db.loadbalancer import models as db_models
from neutron_lbaas.extensions import lbaas_agentschedulerv2
from neutron_lbaas.services.loadbalancer import constants as lb_const
from neutron_lbaas.services.loadbalancer import data_models
from neutron_lbaas.services.loadbalancer.drivers import driver_base

LOG = logging.getLogger(__name__)

LB_SCHEDULERS = 'loadbalancer_schedulers'

AGENT_SCHEDULER_OPTS = [
    cfg.StrOpt('loadbalancer_scheduler_driver',
               default='neutron_lbaas.agent_scheduler.ChanceScheduler',
               help=_('Driver to use for scheduling '
                      'to a default loadbalancer agent')),
]

cfg.CONF.register_opts(AGENT_SCHEDULER_OPTS)


class DriverNotSpecified(n_exc.NeutronException):
    message = _("Device driver for agent should be specified "
                "in plugin driver.")


class LoadBalancerCallbacks(object):

    # history
    #   1.0 Initial version
    target = messaging.Target(version='1.0')

    def __init__(self, plugin):
        super(LoadBalancerCallbacks, self).__init__()
        self.plugin = plugin

    def get_ready_devices(self, context, host=None):
        with context.session.begin(subtransactions=True):
            agents = self.plugin.db.get_lbaas_agents(
                context, filters={'host': [host]})
            if not agents:
                return []
            elif len(agents) > 1:
                LOG.warning(_LW('Multiple lbaas agents found on host %s'),
                            host)
            loadbalancers = self.plugin.db.list_loadbalancers_on_lbaas_agent(
                context, agents[0].id)
            loadbalancer_ids = [
                l['id'] for l in loadbalancers['loadbalancers']]

            qry = context.session.query(
                loadbalancer_dbv2.models.LoadBalancer.id)
            qry = qry.filter(
                loadbalancer_dbv2.models.LoadBalancer.id.in_(
                    loadbalancer_ids))
            qry = qry.filter(
                loadbalancer_dbv2.models.LoadBalancer.provisioning_status.in_(
                    constants.ACTIVE_PENDING_STATUSES))
            up = True  # makes pep8 and sqlalchemy happy
            qry = qry.filter(
                loadbalancer_dbv2.models.LoadBalancer.admin_state_up == up)
            return [id for id, in qry]

    def get_logical_device(self, context, loadbalancer_id=None):
        with context.session.begin(subtransactions=True):
            qry = context.session.query(loadbalancer_dbv2.models.LoadBalancer)
            qry = qry.filter_by(id=loadbalancer_id)
            loadbalancer = qry.one()

            lb = data_models.LoadBalancer.from_sqlalchemy_model(loadbalancer)
            retval = data_models.build_dict(lb)
            vipport = retval['loadbalancer']['vip_port']
            if vipport:
                for fixed_ip in vipport['fixed_ips']:
                    fixed_ip['subnet'] = (
                        self.plugin.db._core_plugin.get_subnet(
                            context,
                            fixed_ip['subnet_id']
                        )
                    )
            if loadbalancer.provider:
                retval['driver'] = (
                    self.plugin.drivers[
                        loadbalancer.provider.provider_name].device_driver)

            return retval

    def loadbalancer_deployed(self, context, loadbalancer_id):
        with context.session.begin(subtransactions=True):
            qry = context.session.query(db_models.LoadBalancer)
            qry = qry.filter_by(id=loadbalancer_id)
            loadbalancer = qry.one()

            # set all resources to active
            if (loadbalancer.provisioning_status in
                    constants.ACTIVE_PENDING_STATUSES):
                loadbalancer.provisioning_status = constants.ACTIVE

            if loadbalancer.listeners:
                for l in loadbalancer.listeners:
                    if (l.provisioning_status in
                            constants.ACTIVE_PENDING_STATUSES):
                        l.provisioning_status = constants.ACTIVE
                    if (l.default_pool
                        and l.default_pool.provisioning_status in
                            constants.ACTIVE_PENDING_STATUSES):
                        l.default_pool.provisioning_status = constants.ACTIVE
                        if l.default_pool.members:
                            for m in l.default_pool.members:
                                if (m.provisioning_status in
                                        constants.ACTIVE_PENDING_STATUSES):
                                    m.provisioning_status = constants.ACTIVE
                        if l.default_pool.health_monitor:
                            hm = l.default_pool.health_monitor
                            ps = hm.provisioning_status
                            if ps in constants.ACTIVE_PENDING_STATUSES:
                                (l.default_pool.health_monitor
                                 .provisioning_status) = constants.ACTIVE

    def update_status(self, context, obj_type, obj_id, status):
        model_mapping = {
            'loadbalancer': db_models.LoadBalancer,
            'pool': db_models.PoolV2,
            'listener': db_models.Listener,
            'member': db_models.MemberV2,
            'healthmonitor': db_models.HealthMonitorV2
        }
        if obj_type not in model_mapping:
            raise n_exc.Invalid(_('Unknown object type: %s') % obj_type)
        try:
            self.plugin.db.update_status(
                context, model_mapping[obj_type], obj_id, status)
        except n_exc.NotFound:
            # update_status may come from agent on an object which was
            # already deleted from db with other request
            LOG.warning(_LW('Cannot update status: %(obj_type)s %(obj_id)s '
                            'not found in the DB, it was probably deleted '
                            'concurrently'),
                        {'obj_type': obj_type, 'obj_id': obj_id})

    def loadbalancer_destroyed(self, context, loadbalancer_id=None):
        """Agent confirmation hook that a load balancer has been destroyed.

        This method exists for subclasses to change the deletion
        behavior.
        """
        pass

    def plug_vip_port(self, context, port_id=None, host=None):
        if not port_id:
            return

        try:
            port = self.plugin.db._core_plugin.get_port(
                context,
                port_id
            )
        except n_exc.PortNotFound:
            LOG.debug('Unable to find port %s to plug.', port_id)
            return

        port['admin_state_up'] = True
        port['device_owner'] = 'neutron:' + constants.LOADBALANCERV2
        port['device_id'] = str(uuid.uuid5(uuid.NAMESPACE_DNS, str(host)))
        port[portbindings.HOST_ID] = host
        self.plugin.db._core_plugin.update_port(
            context,
            port_id,
            {'port': port}
        )

    def unplug_vip_port(self, context, port_id=None, host=None):
        if not port_id:
            return

        try:
            port = self.plugin.db._core_plugin.get_port(
                context,
                port_id
            )
        except n_exc.PortNotFound:
            LOG.debug('Unable to find port %s to unplug. This can occur when '
                      'the Vip has been deleted first.',
                      port_id)
            return

        port['admin_state_up'] = False
        port['device_owner'] = ''
        port['device_id'] = ''

        try:
            self.plugin.db._core_plugin.update_port(
                context,
                port_id,
                {'port': port}
            )

        except n_exc.PortNotFound:
            LOG.debug('Unable to find port %s to unplug.  This can occur when '
                      'the Vip has been deleted first.',
                      port_id)

    def update_loadbalancer_stats(self, context,
                                  loadbalancer_id=None,
                                  stats=None):
        self.plugin.update_loadbalancer_stats(context, loadbalancer_id, stats)


class LoadBalancerAgentApi(object):
    """Plugin side of plugin to agent RPC API."""

    # history
    #   1.0 Initial version
    #

    def __init__(self, topic):
        target = messaging.Target(topic=topic, version='2.0')
        self.client = n_rpc.get_client(target)

    def create_loadbalancer(self, context, loadbalancer, host, driver_name):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'create_loadbalancer',
                   loadbalancer=loadbalancer, driver_name=driver_name)

    def update_loadbalancer(self, context, old_loadbalancer,
                            loadbalancer, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'update_loadbalancer',
                   old_loadbalancer=old_loadbalancer,
                   loadbalancer=loadbalancer)

    def delete_loadbalancer(self, context, loadbalancer, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'delete_loadbalancer',
                   loadbalancer=loadbalancer)

    def create_listener(self, context, listener, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'create_listener', listener=listener)

    def update_listener(self, context, old_listener,
                        listener, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'update_listener',
                   old_listener=old_listener,
                   loadbalancer=listener)

    def delete_listener(self, context, listener, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'delete_listener',
                   listener=listener)

    def create_pool(self, context, pool, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'create_pool', pool=pool)

    def update_pool(self, context, old_pool, pool, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'update_pool', old_pool=old_pool, pool=pool)

    def delete_pool(self, context, pool, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'delete_pool', pool=pool)

    def create_member(self, context, member, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'create_member', member=member)

    def update_member(self, context, old_member, member, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'update_member', old_member=old_member,
                   member=member)

    def delete_member(self, context, member, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'delete_member', member=member)

    def create_health_monitor(self, context, health_monitor, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'create_health_monitor',
                   health_monitor=health_monitor)

    def update_health_monitor(self, context, old_health_monitor,
                              health_monitor, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'update_health_monitor',
                   old_health_monitor=old_health_monitor,
                   health_monitor=health_monitor)

    def delete_health_monitor(self, context, health_monitor, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'delete_health_monitor',
                   health_monitor=health_monitor)

    def agent_updated(self, context, admin_state_up, host):
        cctxt = self.client.prepare(server=host)
        cctxt.cast(context, 'agent_updated',
                   payload={'admin_state_up': admin_state_up})


class LoadBalancerManager(driver_base.BaseLoadBalancerManager):

    def update(self, context, old_loadbalancer, loadbalancer):
        super(LoadBalancerManager, self).update(context, old_loadbalancer,
                                                loadbalancer)
        agent = self.driver.get_loadbalancer_agent(context, loadbalancer.id)
        self.driver.agent_rpc.update_loadbalancer(
            context, old_loadbalancer.to_dict(), loadbalancer.to_dict(),
            agent['host'])

    def create(self, context, loadbalancer):
        super(LoadBalancerManager, self).create(context, loadbalancer)
        agent = self.driver.loadbalancer_scheduler.schedule(
            self.driver.plugin, context, loadbalancer,
            self.driver.device_driver)
        if not agent:
            raise lbaas_agentschedulerv2.NoEligibleLbaasAgent(
                loadbalancer_id=loadbalancer.id)
        self.driver.agent_rpc.create_loadbalancer(
            context, data_models.build_dict(loadbalancer), agent['host'],
            self.driver.device_driver)

    def delete(self, context, loadbalancer):
        super(LoadBalancerManager, self).delete(context, loadbalancer)
        agent = self.driver.get_loadbalancer_agent(context, loadbalancer.id)
        self.driver.agent_rpc.delete_loadbalancer(
            context, data_models.build_dict(loadbalancer), agent['host'])

    def stats(self, context, loadbalancer):
        pass

    def refresh(self, context, loadbalancer):
        pass


class ListenerManager(driver_base.BaseListenerManager):

    def update(self, context, old_listener, listener):
        super(ListenerManager, self).update(
            context, old_listener.to_dict(), listener.to_dict())
        agent = self.driver.get_loadbalancer_agent(
            context, listener.loadbalancer.id)
        self.driver.agent_rpc.update_listener(
            context, old_listener.to_dict(), listener.to_dict(),
            agent['host'])

    def create(self, context, listener):
        super(ListenerManager, self).create(context, listener)
        agent = self.driver.get_loadbalancer_agent(
            context, listener.loadbalancer.id)
        self.driver.agent_rpc.create_listener(
            context, listener.to_dict(), agent['host'])

    def delete(self, context, listener):
        super(ListenerManager, self).delete(context, listener)
        agent = self.driver.get_loadbalancer_agent(context, listener.id)
        self.driver.agent_rpc.delete_listener(context, listener.to_dict(),
                                              agent['host'])

    def refresh(self, context, listener):
        pass


class PoolManager(driver_base.BasePoolManager):

    def update(self, context, old_pool, pool):
        super(PoolManager, self).update(context, old_pool, pool)
        agent = self.driver.get_loadbalancer_agent(
            context, pool.listener.loadbalancer.id)
        self.driver.agent_rpc.update_pool(
            context, old_pool.to_dict(), pool.to_dict(), agent['host'])

    def create(self, context, pool):
        super(PoolManager, self).delete(context, pool)
        agent = self.driver.get_loadbalancer_agent(
            context, pool.listener.loadbalancer.id)
        self.driver.agent_rpc.create_pool(context, pool.to_dict(),
                                          agent['host'])

    def delete(self, context, pool):
        super(PoolManager, self).delete(context, pool)
        agent = self.driver.get_loadbalancer_agent(
            context, pool.listener.loadbalancer.id)
        self.driver.agent_rpc.delete_pool(context, pool.to_dict(),
                                          agent['host'])


class MemberManager(driver_base.BaseMemberManager):

    def update(self, context, old_member, member):
        super(MemberManager, self).update(context, old_member, member)
        agent = self.driver.get_loadbalancer_agent(
            context, member.pool.listener.loadbalancer.id)
        self.driver.agent_rpc.update_member(context, old_member.to_dict(),
                                            member.to_dict(), agent['host'])

    def create(self, context, member):
        super(MemberManager, self).create(context, member)
        agent = self.driver.get_loadbalancer_agent(
            context, member.pool.listener.loadbalancer.id)
        self.driver.agent_rpc.create_member(context, member.to_dict(),
                                            agent['host'])

    def delete(self, context, member):
        super(MemberManager, self).delete(context, member)
        agent = self.driver.get_loadbalancer_agent(
            context, member.pool.listener.loadbalancer.id)
        self.driver.agent_rpc.delete_member(context, member.to_dict(),
                                            agent['host'])


class HealthMonitorManager(driver_base.BaseHealthMonitorManager):

    def update(self, context, old_healthmonitor, healthmonitor):
        super(HealthMonitorManager, self).update(
            context, old_healthmonitor, healthmonitor)
        agent = self.driver.get_loadbalancer_agent(
            context, healthmonitor.pool.listener.loadbalancer.id)
        self.driver.agent_rpc.update_health_monitor(
            context, old_healthmonitor.to_dict(), healthmonitor.to_dict(),
            agent['host'])

    def create(self, context, healthmonitor):
        super(HealthMonitorManager, self).create(context, healthmonitor)
        agent = self.driver.get_loadbalancer_agent(
            context, healthmonitor.pool.listener.loadbalancer.id)
        self.driver.agent_rpc.create_health_monitor(
            context, healthmonitor.to_dict(), agent['host'])

    def delete(self, context, healthmonitor):
        super(HealthMonitorManager, self).delete(context, healthmonitor)
        agent = self.driver.get_loadbalancer_agent(
            context, healthmonitor.pool.listener.loadbalancer.id)
        self.driver.agent_rpc.delete_health_monitor(
            context, healthmonitor.to_dict(), agent['host'])


class AgentDriverBase(driver_base.LoadBalancerBaseDriver):

    # name of device driver that should be used by the agent;
    # vendor specific plugin drivers must override it;
    device_driver = None

    def __init__(self, plugin):
        super(AgentDriverBase, self).__init__(plugin)
        if not self.device_driver:
            raise DriverNotSpecified()

        self.load_balancer = LoadBalancerManager(self)
        self.listener = ListenerManager(self)
        self.pool = PoolManager(self)
        self.member = MemberManager(self)
        self.health_monitor = HealthMonitorManager(self)

        self.agent_rpc = LoadBalancerAgentApi(lb_const.LOADBALANCER_AGENTV2)

        self._set_callbacks_on_plugin()
        # Setting this on the db because the plugin no longer inherts from
        # database classes, the db does.
        self.plugin.db.agent_notifiers.update(
            {lb_const.AGENT_TYPE_LOADBALANCERV2: self.agent_rpc})

        lb_sched_driver = provconf.get_provider_driver_class(
            cfg.CONF.loadbalancer_scheduler_driver, LB_SCHEDULERS)
        self.loadbalancer_scheduler = importutils.import_object(
            lb_sched_driver)

    def _set_callbacks_on_plugin(self):
        # other agent based plugin driver might already set callbacks on plugin
        if hasattr(self.plugin, 'agent_callbacks'):
            return

        self.plugin.agent_endpoints = [
            LoadBalancerCallbacks(self.plugin),
            agents_db.AgentExtRpcCallback(self.plugin.db)
        ]
        self.plugin.conn = n_rpc.create_connection(new=True)
        self.plugin.conn.create_consumer(
            lb_const.LOADBALANCER_PLUGINV2,
            self.plugin.agent_endpoints,
            fanout=False)
        self.plugin.conn.consume_in_threads()

    def get_loadbalancer_agent(self, context, loadbalancer_id):
        agent = self.plugin.db.get_agent_hosting_loadbalancer(
            context, loadbalancer_id)
        if not agent:
            raise lbaas_agentschedulerv2.NoActiveLbaasAgent(
                loadbalancer_id=loadbalancer_id)
        return agent['agent']
