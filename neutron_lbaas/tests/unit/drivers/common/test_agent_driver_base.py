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

import contextlib
import mock

from neutron import context
from neutron.db import servicetype_db as st_db
from neutron.extensions import portbindings
from neutron import manager
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants
from neutron.tests.unit import testlib_api
from six import moves

from neutron_lbaas.db.loadbalancer import loadbalancer_dbv2 as ldb
from neutron_lbaas.db.loadbalancer import models
from neutron_lbaas.drivers.common import agent_driver_base
from neutron_lbaas.extensions import loadbalancerv2
from neutron_lbaas.services.loadbalancer import data_models
from neutron_lbaas.tests import base
from neutron_lbaas.tests.unit.db.loadbalancer import test_db_loadbalancerv2


# TODO(ptoohill): refactor for mock patch decorators...


class TestLoadBalancerPluginBase(test_db_loadbalancerv2.LbaasPluginDbTestCase):

    def setUp(self):
        def reset_device_driver():
            agent_driver_base.AgentDriverBase.device_driver = None
        self.addCleanup(reset_device_driver)

        self.mock_importer = mock.patch.object(
            agent_driver_base, 'importutils').start()

        # needed to reload provider configuration
        st_db.ServiceTypeManager._instance = None
        agent_driver_base.AgentDriverBase.device_driver = 'dummy'
        super(TestLoadBalancerPluginBase, self).setUp(
            lbaas_provider=('LOADBALANCERV2:lbaas:neutron_lbaas.drivers.'
                            'common.agent_driver_base.'
                            'AgentDriverBase:default'))

        # we need access to loaded plugins to modify models
        loaded_plugins = manager.NeutronManager().get_service_plugins()

        self.plugin_instance = loaded_plugins[constants.LOADBALANCERV2]


class TestLoadBalancerCallbacks(TestLoadBalancerPluginBase):
    def setUp(self):
        super(TestLoadBalancerCallbacks, self).setUp()

        self.callbacks = agent_driver_base.LoadBalancerCallbacks(
            self.plugin_instance
        )
        get_lbaas_agents_patcher = mock.patch(
            'neutron_lbaas.agent_scheduler.LbaasAgentSchedulerDbMixin.'
            'get_lbaas_agents')
        get_lbaas_agents_patcher.start()

    def test_get_ready_devices(self):
        with self.loadbalancer() as loadbalancer:
            self.plugin_instance.db.update_loadbalancer_provisioning_status(
                context.get_admin_context(),
                loadbalancer['loadbalancer']['id'])
            with mock.patch(
                    'neutron_lbaas.agent_scheduler.LbaasAgentSchedulerDbMixin.'
                    'list_loadbalancers_on_lbaas_agent') as mock_agent_lbs:
                mock_agent_lbs.return_value = {
                    'loadbalancers': [
                        {'id': loadbalancer['loadbalancer']['id']}]}
                ready = self.callbacks.get_ready_devices(
                    context.get_admin_context(),
                )
                self.assertEqual(ready, [loadbalancer['loadbalancer']['id']])

    def test_get_ready_devices_multiple_listeners_and_loadbalancers(self):
        ctx = context.get_admin_context()

        # add 3 load balancers and 2 listeners directly to DB
        # to create 2 "ready" devices and one load balancer without listener
        loadbalancers = []
        for i in moves.xrange(3):
            loadbalancers.append(ldb.models.LoadBalancer(
                id=uuidutils.generate_uuid(), vip_subnet_id=self._subnet_id,
                provisioning_status=constants.ACTIVE, admin_state_up=True,
                operating_status=constants.ACTIVE))
            ctx.session.add(loadbalancers[i])

        listener0 = ldb.models.Listener(
            id=uuidutils.generate_uuid(), protocol="HTTP",
            loadbalancer_id=loadbalancers[0].id,
            provisioning_status=constants.ACTIVE, admin_state_up=True,
            connection_limit=3, protocol_port=80,
            operating_status=constants.ACTIVE)
        ctx.session.add(listener0)
        loadbalancers[0].listener_id = listener0.id

        listener1 = ldb.models.Listener(
            id=uuidutils.generate_uuid(), protocol="HTTP",
            loadbalancer_id=loadbalancers[1].id,
            provisioning_status=constants.ACTIVE, admin_state_up=True,
            connection_limit=3, protocol_port=80,
            operating_status=constants.ACTIVE)
        ctx.session.add(listener1)
        loadbalancers[1].listener_id = listener1.id

        ctx.session.flush()

        self.assertEqual(ctx.session.query(ldb.models.LoadBalancer).count(), 3)
        self.assertEqual(ctx.session.query(ldb.models.Listener).count(), 2)
        with mock.patch(
                'neutron_lbaas.agent_scheduler.LbaasAgentSchedulerDbMixin'
                '.list_loadbalancers_on_lbaas_agent') as mock_agent_lbs:
            mock_agent_lbs.return_value = {'loadbalancers': [
                {'id': loadbalancers[0].id}, {'id': loadbalancers[1].id},
                {'id': loadbalancers[2].id}]}
            ready = self.callbacks.get_ready_devices(ctx)
            self.assertEqual(len(ready), 3)
            self.assertIn(loadbalancers[0].id, ready)
            self.assertIn(loadbalancers[1].id, ready)
            self.assertIn(loadbalancers[2].id, ready)
        # cleanup
        ctx.session.query(ldb.models.Listener).delete()
        ctx.session.query(ldb.models.LoadBalancer).delete()

    def test_get_ready_devices_inactive_loadbalancer(self):
        with self.loadbalancer() as loadbalancer:
            self.plugin_instance.db.update_loadbalancer_provisioning_status(
                context.get_admin_context(),
                loadbalancer['loadbalancer']['id'])
            # set the loadbalancer inactive need to use plugin directly since
            # status is not tenant mutable
            self.plugin_instance.db.update_loadbalancer(
                context.get_admin_context(),
                loadbalancer['loadbalancer']['id'],
                {'loadbalancer': {'provisioning_status': constants.INACTIVE}}
            )
            with mock.patch(
                    'neutron_lbaas.agent_scheduler.LbaasAgentSchedulerDbMixin.'
                    'list_loadbalancers_on_lbaas_agent') as mock_agent_lbs:
                mock_agent_lbs.return_value = {
                    'loadbalancers': [{'id': loadbalancer[
                        'loadbalancer']['id']}]}
                ready = self.callbacks.get_ready_devices(
                    context.get_admin_context(),
                )
                self.assertEqual([loadbalancer['loadbalancer']['id']],
                                 ready)

    def test_get_logical_device_non_active(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            ctx = context.get_admin_context()
            for status in ('INACTIVE', 'PENDING_CREATE', 'PENDING_UPDATE'):
                self.plugin_instance.db.update_status(
                    ctx, ldb.models.LoadBalancer,
                    loadbalancer['loadbalancer']['id'], status)
                loadbalancer['loadbalancer'][
                    'provisioning_status'] = status
                lb = data_models.build_dict(
                        self.plugin_instance.db.get_loadbalancer(
                            ctx, loadbalancer['loadbalancer']['id']))
                lb['driver'] = 'dummy'
                vipport = lb['loadbalancer']['vip_port']
                for fixed_ip in vipport['fixed_ips']:
                    fixed_ip['subnet'] = (
                        self.plugin_instance.db._core_plugin.get_subnet(
                            ctx,
                            fixed_ip['subnet_id']
                        )
                    )
                expected = lb

                logical_config = self.callbacks.get_logical_device(
                    ctx, loadbalancer['loadbalancer']['id']
                )
                self.assertEqual(expected, logical_config)

    def test_get_logical_device_active(self):
        ctx = context.get_admin_context()

        member = ldb.models.MemberV2(
            id=uuidutils.generate_uuid(), weight=5,
            provisioning_status=constants.ACTIVE,
            admin_state_up=True, address="10.0.0.1",
            protocol_port=80,
            operating_status=constants.ACTIVE)
        ctx.session.add(member)

        pool = ldb.models.PoolV2(
            id=uuidutils.generate_uuid(), protocol="HTTP",
            provisioning_status=constants.ACTIVE, admin_state_up=True,
            lb_algorithm='ROUND_ROBIN',
            operating_status=constants.ACTIVE, members=[member])
        ctx.session.add(pool)

        listener = ldb.models.Listener(
            id=uuidutils.generate_uuid(), protocol="HTTP",
            provisioning_status=constants.ACTIVE, admin_state_up=True,
            connection_limit=3, protocol_port=80,
            operating_status=constants.ACTIVE, default_pool=pool)
        ctx.session.add(listener)

        loadbalancer = ldb.models.LoadBalancer(
            id=uuidutils.generate_uuid(), vip_subnet_id=self._subnet_id,
            provisioning_status=constants.ACTIVE, admin_state_up=True,
            operating_status=constants.ACTIVE, listeners=[listener])
        ctx.session.add(loadbalancer)

        ctx.session.flush()

        # build the expected
        lb = data_models.build_dict(
            self.plugin_instance.db.get_loadbalancer(
                ctx, loadbalancer['id']))
        expected = lb

        logical_config = self.callbacks.get_logical_device(
            ctx, loadbalancer['id']
        )

        self.assertEqual(logical_config, expected)

    def test_get_logical_device_inactive_member(self):
        ctx = context.get_admin_context()

        member = ldb.models.MemberV2(
            id=uuidutils.generate_uuid(), weight=5,
            provisioning_status=constants.INACTIVE,
            admin_state_up=True, address="10.0.0.1",
            protocol_port=80,
            operating_status=constants.INACTIVE)
        ctx.session.add(member)

        pool = ldb.models.PoolV2(
            id=uuidutils.generate_uuid(), protocol="HTTP",
            provisioning_status=constants.ACTIVE, admin_state_up=True,
            lb_algorithm='ROUND_ROBIN',
            operating_status=constants.ACTIVE, members=[member])
        ctx.session.add(pool)

        listener = ldb.models.Listener(
            id=uuidutils.generate_uuid(), protocol="HTTP",
            provisioning_status=constants.ACTIVE, admin_state_up=True,
            connection_limit=3, protocol_port=80,
            operating_status=constants.ACTIVE, default_pool=pool)
        ctx.session.add(listener)

        loadbalancer = ldb.models.LoadBalancer(
            id=uuidutils.generate_uuid(), vip_subnet_id=self._subnet_id,
            provisioning_status=constants.ACTIVE, admin_state_up=True,
            operating_status=constants.ACTIVE, listeners=[listener])
        ctx.session.add(loadbalancer)

        ctx.session.flush()

        # build the expected
        lb = data_models.build_dict(
            self.plugin_instance.db.get_loadbalancer(
                ctx, loadbalancer['id']))
        expected = lb

        logical_config = self.callbacks.get_logical_device(
            ctx, loadbalancer['id']
        )

        self.assertEqual(logical_config, expected)

    def _update_port_test_helper(self, expected, func, **kwargs):
        core = self.plugin_instance.db._core_plugin

        with self.loadbalancer() as loadbalancer:
            self.plugin_instance.db.update_loadbalancer_provisioning_status(
                context.get_admin_context(),
                loadbalancer['loadbalancer']['id'])
            ctx = context.get_admin_context()
            logical_config = self.callbacks.get_logical_device(
                ctx, loadbalancer['loadbalancer']['id'])
            func(ctx, port_id=logical_config['loadbalancer']['vip_port_id'],
                 **kwargs)

            db_port = core.get_port(
                ctx, logical_config['loadbalancer']['vip_port_id'])

            for k, v in expected.iteritems():
                self.assertEqual(db_port[k], v)

    def test_plug_vip_port(self):
        exp = {
            'device_owner': 'neutron:' + constants.LOADBALANCERV2,
            'device_id': 'c596ce11-db30-5c72-8243-15acaae8690f',
            'admin_state_up': True
        }
        self._update_port_test_helper(
            exp,
            self.callbacks.plug_vip_port,
            host='host'
        )

    def test_plug_vip_port_mock_with_host(self):
        exp = {
            'device_owner': 'neutron:' + constants.LOADBALANCERV2,
            'device_id': 'c596ce11-db30-5c72-8243-15acaae8690f',
            'admin_state_up': True,
            portbindings.HOST_ID: 'host'
        }
        with mock.patch.object(
                self.plugin.db._core_plugin,
                'update_port') as mock_update_port:
            with self.loadbalancer() as loadbalancer:

                ctx = context.get_admin_context()
                self.callbacks.update_status(
                    ctx,
                    'loadbalancer',
                    loadbalancer['loadbalancer']['id'],
                    'ACTIVE')
                (self.plugin_instance.db
                 .update_loadbalancer_provisioning_status(ctx,
                                                          loadbalancer[
                                                              'loadbalancer'][
                                                              'id']))
                logical_config = self.callbacks.get_logical_device(
                    ctx, loadbalancer['loadbalancer']['id'])
                self.callbacks.plug_vip_port(
                    ctx, port_id=logical_config['loadbalancer']['vip_port_id'],
                    host='host')
            mock_update_port.assert_called_once_with(
                ctx, logical_config['loadbalancer']['vip_port_id'],
                {'port': testlib_api.SubDictMatch(exp)})

    def test_unplug_vip_port(self):
        exp = {
            'device_owner': '',
            'device_id': '',
            'admin_state_up': False
        }
        self._update_port_test_helper(
            exp,
            self.callbacks.unplug_vip_port,
            host='host'
        )

    def test_loadbalancer_deployed(self):
        with self.loadbalancer() as loadbalancer:
            ctx = context.get_admin_context()

            l = self.plugin_instance.db.get_loadbalancer(
                ctx, loadbalancer['loadbalancer']['id'])
            self.assertEqual('PENDING_CREATE', l.provisioning_status)

            self.callbacks.loadbalancer_deployed(
                ctx, loadbalancer['loadbalancer']['id'])

            l = self.plugin_instance.db.get_loadbalancer(
                ctx, loadbalancer['loadbalancer']['id'])
            self.assertEqual('ACTIVE', l.provisioning_status)

    def test_listener_deployed(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self.plugin_instance.db.update_loadbalancer_provisioning_status(
                context.get_admin_context(),
                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id']) as listener:
                ctx = context.get_admin_context()

                l = self.plugin_instance.db.get_loadbalancer(
                    ctx, loadbalancer['loadbalancer']['id'])
                self.assertEqual('PENDING_UPDATE', l.provisioning_status)

                ll = self.plugin_instance.db.get_listener(
                    ctx, listener['listener']['id'])
                self.assertEqual('PENDING_CREATE', ll.provisioning_status)

                self.callbacks.loadbalancer_deployed(
                    ctx, loadbalancer['loadbalancer']['id'])

                l = self.plugin_instance.db.get_loadbalancer(
                    ctx, loadbalancer['loadbalancer']['id'])
                self.assertEqual('ACTIVE', l.provisioning_status)
                ll = self.plugin_instance.db.get_listener(
                    ctx, listener['listener']['id'])
                self.assertEqual('ACTIVE', ll.provisioning_status)

    def test_update_status_loadbalancer(self):
        with self.loadbalancer() as loadbalancer:
            loadbalancer_id = loadbalancer['loadbalancer']['id']
            ctx = context.get_admin_context()
            l = self.plugin_instance.db.get_loadbalancer(ctx, loadbalancer_id)
            self.assertEqual('PENDING_CREATE', l.provisioning_status)
            self.callbacks.update_status(ctx, 'loadbalancer',
                                         loadbalancer_id, 'ACTIVE')
            l = self.plugin_instance.db.get_loadbalancer(ctx, loadbalancer_id)
            self.assertEqual('ACTIVE', l.provisioning_status)

    def test_update_status_loadbalancer_deleted_already(self):
        with mock.patch.object(agent_driver_base, 'LOG') as mock_log:
            loadbalancer_id = 'deleted_lb'
            ctx = context.get_admin_context()
            self.assertRaises(loadbalancerv2.EntityNotFound,
                              self.plugin_instance.get_pool, ctx,
                              loadbalancer_id)
            self.callbacks.update_status(ctx, 'loadbalancer',
                                         loadbalancer_id, 'ACTIVE')
            self.assertTrue(mock_log.warning.called)


class TestLoadBalancerAgentApi(base.BaseTestCase):
    def setUp(self):
        super(TestLoadBalancerAgentApi, self).setUp()

        self.api = agent_driver_base.LoadBalancerAgentApi('topic')

    def test_init(self):
        self.assertEqual(self.api.client.target.topic, 'topic')

    def _call_test_helper(self, method_name, method_args):
        with contextlib.nested(
            mock.patch.object(self.api.client, 'cast'),
            mock.patch.object(self.api.client, 'prepare'),
        ) as (
            rpc_mock, prepare_mock
        ):
            prepare_mock.return_value = self.api.client
            getattr(self.api, method_name)(mock.sentinel.context,
                                           host='host',
                                           **method_args)

        prepare_args = {'server': 'host'}
        prepare_mock.assert_called_once_with(**prepare_args)

        if method_name == 'agent_updated':
            method_args = {'payload': method_args}
        rpc_mock.assert_called_once_with(mock.sentinel.context, method_name,
                                         **method_args)

    def test_agent_updated(self):
        self._call_test_helper('agent_updated', {'admin_state_up': 'test'})

    def test_create_pool(self):
        self._call_test_helper('create_pool', {'pool': 'test'})

    def test_update_pool(self):
        self._call_test_helper('update_pool', {'old_pool': 'test',
                                               'pool': 'test'})

    def test_delete_pool(self):
        self._call_test_helper('delete_pool', {'pool': 'test'})

    def test_create_loadbalancer(self):
        self._call_test_helper('create_loadbalancer', {'loadbalancer': 'test',
                                                       'driver_name': 'dummy'})

    def test_update_loadbalancer(self):
        self._call_test_helper('update_loadbalancer', {
            'old_loadbalancer': 'test', 'loadbalancer': 'test'})

    def test_delete_loadbalancer(self):
        self._call_test_helper('delete_loadbalancer', {'loadbalancer': 'test'})

    def test_create_member(self):
        self._call_test_helper('create_member', {'member': 'test'})

    def test_update_member(self):
        self._call_test_helper('update_member', {'old_member': 'test',
                                                 'member': 'test'})

    def test_delete_member(self):
        self._call_test_helper('delete_member', {'member': 'test'})

    def test_create_monitor(self):
        self._call_test_helper('create_health_monitor',
                               {'health_monitor': 'test'})

    def test_update_monitor(self):
        self._call_test_helper('update_health_monitor',
                               {'old_health_monitor': 'test',
                                'health_monitor': 'test'})

    def test_delete_monitor(self):
        self._call_test_helper('delete_health_monitor',
                               {'health_monitor': 'test'})


class TestLoadBalancerPluginNotificationWrapper(TestLoadBalancerPluginBase):
    def setUp(self):
        self.log = mock.patch.object(agent_driver_base, 'LOG')
        api_cls = mock.patch.object(agent_driver_base,
                                    'LoadBalancerAgentApi').start()
        super(TestLoadBalancerPluginNotificationWrapper, self).setUp()
        self.mock_api = api_cls.return_value

        self.mock_get_driver = mock.patch.object(self.plugin_instance,
                                                 '_get_driver')
        self.mock_get_driver.return_value = (
            agent_driver_base.AgentDriverBase(self.plugin_instance))

    def _convert_unicode_dict(self, indict):
        outdict = {}
        for k, v in indict.items():
            ky = k
            vv = v
            if isinstance(k, unicode):
                ky = k.encode('utf8')
            if isinstance(v, unicode):
                vv = v.encode('utf8')
            outdict[ky] = vv
        return outdict

    def _update_status(self, model, status, id):
        ctx = context.get_admin_context()
        self.plugin_instance.db.update_status(
            ctx,
            model,
            id,
            provisioning_status=status
        )

    def test_create_loadbalancer(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            ctx = context.get_admin_context()
            lb = data_models.build_dict(
                self.plugin_instance.db.get_loadbalancer(
                    ctx, loadbalancer['loadbalancer']['id']))
            self.mock_api.create_loadbalancer.assert_called_once_with(
                mock.ANY,
                lb,
                mock.ANY,
                'dummy'
            )

    def test_update_loadbalancer(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            ctx = context.get_admin_context()
            self.plugin_instance.db.update_loadbalancer_provisioning_status(
                ctx,
                loadbalancer['loadbalancer']['id'])

            old_lb = self.plugin_instance.db.get_loadbalancer(
                ctx, loadbalancer['loadbalancer']['id']).to_dict()
            loadbalancer['loadbalancer'].pop('provisioning_status')
            self.plugin_instance.update_loadbalancer(
                ctx,
                loadbalancer['loadbalancer']['id'],
                loadbalancer
            )
            new_lb = self.plugin_instance.db.get_loadbalancer(
                    ctx, loadbalancer['loadbalancer']['id']).to_dict()

            self.mock_api.update_loadbalancer.assert_called_once_with(
                mock.ANY,
                old_lb,
                new_lb,
                'host'
            )

            self.assertEqual(
                new_lb['provisioning_status'],
                constants.PENDING_UPDATE
            )

    def test_delete_loadbalancer(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            ctx = context.get_admin_context()
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            self.plugin_instance.delete_loadbalancer(
                ctx, loadbalancer['loadbalancer']['id'])

            lb = data_models.build_dict(
                self.plugin_instance.db.get_loadbalancer(
                    ctx, loadbalancer['loadbalancer']['id']))

            # LB should be in PENDING_DELETE status
            self.assertEqual(
                lb['loadbalancer']['provisioning_status'],
                constants.PENDING_DELETE
            )
            # Expected to be called in ACTIVE status?
            lb['loadbalancer']['provisioning_status'] = 'ACTIVE'
            self.mock_api.delete_loadbalancer.assert_called_once_with(
                mock.ANY,
                lb,
                'host'
            )

    def test_create_listener(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            ctx = context.get_admin_context()
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                l = self.plugin_instance.db.get_listener(
                    ctx, listener['listener']['id'])
                self.mock_api.create_listener.assert_called_once_with(
                    mock.ANY,
                    self._convert_unicode_dict(l.to_dict()),
                    'host'
                )

    def test_update_listener_non_active(self):
        # Resources are checked for status verification before
        # it reaches driver. Verify intended behavior
        #
        # with self.loadbalancer(no_delete=True) as loadbalancer:
        #     ctx = context.get_admin_context()
        #     self.plugin_instance.db.update_loadbalancer_provisioning_status(
        #         ctx,
        #         loadbalancer['loadbalancer']['id'])
        #     with self.listener(
        #             loadbalancer_id=loadbalancer[
        #                 'loadbalancer']['id'], no_delete=True) as listener:
        #         listener['listener']['provisioning_status'] = 'INACTIVE'
        #         ctx = context.get_admin_context()
        #         orig_listener = self.plugin_instance.db.get_listener(
        #             ctx, listener['listener']['id'])
        #         self.plugin_instance.update_listener(
        #             ctx, listener['listener']['id'], listener)
        #         self.mock_api.delete_listener.assert_called_once_with(
        #             mock.ANY,
        #             self._convert_unicode_dict(orig_listener),
        #             'host')
        pass

    def test_update_listener(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                ctx = context.get_admin_context()
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                old_listener = self.plugin_instance.db.get_listener(
                    ctx, listener['listener']['id'])
                self.plugin_instance.update_listener(
                    ctx, listener['listener']['id'], listener)
                new_listener = self.plugin_instance.db.get_listener(
                    ctx, listener['listener']['id'])
                self.mock_api.update_listener.assert_called_once_with(
                    mock.ANY,
                    self._convert_unicode_dict(old_listener.to_dict()),
                    self._convert_unicode_dict(new_listener.to_dict()),
                    'host')

    def test_delete_listener(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                ctx = context.get_admin_context()

                self.plugin_instance.delete_listener(
                    ctx, listener['listener']['id'])

                l = self.plugin_instance.db.get_listener(
                    ctx, listener['listener']['id']).to_dict()

                # LB should be in PENDING_DELETE status
                self.assertEqual(
                    l['provisioning_status'],
                    constants.PENDING_DELETE
                )
                self.mock_api.delete_listener.assert_called_once_with(
                    mock.ANY,
                    l,
                    'host'
                )

    def test_create_pool(self):
        ctx = context.get_admin_context()
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                with self.pool(listener_id=listener['listener']['id'],
                               no_delete=True) as pool:
                    cpool = self.plugin_instance.db.get_pool(
                        ctx, pool['pool']['id'])
                    self.mock_api.create_pool.assert_called_once_with(
                        mock.ANY,
                        self._convert_unicode_dict(cpool.to_dict()),
                        'host'
                    )

    def test_update_pool(self):
        ctx = context.get_admin_context()
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                with self.pool(listener_id=listener['listener']['id'],
                               no_delete=True) as pool:
                    self._update_status(models.Listener, constants.ACTIVE,
                                        listener['listener']['id'])
                    self._update_status(models.LoadBalancer, constants.ACTIVE,
                                        loadbalancer['loadbalancer']['id'])
                    self._update_status(models.PoolV2, constants.ACTIVE,
                                        pool['pool']['id'])
                    old_pool = self.plugin_instance.db.get_pool(
                        ctx, pool['pool']['id'])
                    self.plugin_instance.update_pool(
                        ctx, pool['pool']['id'], pool)
                    new_pool = self.plugin_instance.db.get_pool(
                        ctx, pool['pool']['id'])
                    self.mock_api.update_pool.assert_called_once_with(
                        mock.ANY,
                        self._convert_unicode_dict(old_pool.to_dict()),
                        self._convert_unicode_dict(new_pool.to_dict()),
                        'host')

    def test_delete_pool(self):
        ctx = context.get_admin_context()
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                with self.pool(listener_id=listener['listener']['id'],
                               no_delete=True) as pool:
                    self._update_status(models.Listener, constants.ACTIVE,
                                        listener['listener']['id'])
                    self._update_status(models.LoadBalancer, constants.ACTIVE,
                                        loadbalancer['loadbalancer']['id'])
                    self._update_status(models.PoolV2, constants.ACTIVE,
                                        pool['pool']['id'])

                    self.plugin_instance.delete_pool(
                        ctx, pool['pool']['id'])
                    cpool = self.plugin_instance.db.get_pool(
                        ctx, pool['pool']['id'])
                    self.mock_api.delete_pool.assert_called_once_with(
                        mock.ANY,
                        self._convert_unicode_dict(cpool.to_dict()),
                        'host')

    def test_create_member(self):
        ctx = context.get_admin_context()
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                with self.pool(listener_id=listener['listener']['id'],
                               no_delete=True) as pool:
                    self._update_status(models.Listener, constants.ACTIVE,
                                        listener['listener']['id'])
                    self._update_status(models.LoadBalancer, constants.ACTIVE,
                                        loadbalancer['loadbalancer']['id'])
                    self._update_status(models.PoolV2, constants.ACTIVE,
                                        pool['pool']['id'])
                    with self.subnet(cidr='11.0.0.0/24') as subnet:
                        with self.member(pool_id=pool['pool']['id'],
                                         subnet=subnet,
                                         no_delete=True) as member:
                            self._update_status(models.MemberV2,
                                                constants.ACTIVE,
                                                member['member']['id'])
                            nmember = self.plugin_instance.db.get_pool_member(
                                ctx, member['member']['id']).to_dict()
                            pc = constants.PENDING_CREATE
                            nmember['provisioning_status'] = pc
                            pm = self.mock_api.create_member
                            pm.assert_called_once_with(
                                mock.ANY,
                                self._convert_unicode_dict(nmember),
                                'host'
                            )

    def test_update_member(self):
        ctx = context.get_admin_context()
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                with self.pool(listener_id=listener['listener']['id'],
                               no_delete=True) as pool:
                    self._update_status(models.Listener, constants.ACTIVE,
                                        listener['listener']['id'])
                    self._update_status(models.LoadBalancer, constants.ACTIVE,
                                        loadbalancer['loadbalancer']['id'])
                    self._update_status(models.PoolV2, constants.ACTIVE,
                                        pool['pool']['id'])
                    with self.subnet(cidr='11.0.0.0/24') as subnet:
                        with self.member(pool_id=pool['pool']['id'],
                                         subnet=subnet,
                                         no_delete=True) as member:
                            self._update_status(models.Listener,
                                                constants.ACTIVE,
                                                listener['listener']['id'])
                            self._update_status(models.LoadBalancer,
                                                constants.ACTIVE,
                                                loadbalancer[
                                                    'loadbalancer']['id'])
                            self._update_status(models.PoolV2,
                                                constants.ACTIVE,
                                                pool['pool']['id'])
                            self._update_status(models.MemberV2,
                                                constants.ACTIVE,
                                                member['member']['id'])
                            nmember = self.plugin_instance.db.get_pool_member(
                                ctx, member['member']['id']).to_dict()
                            pc = constants.PENDING_CREATE
                            nmember['provisioning_status'] = pc
                            gpm = self.plugin_instance.db.get_pool_member
                            old_member = gpm(
                                ctx, member['member']['id'])
                            self.plugin_instance.update_pool_member(
                                ctx, member['member']['id'],
                                pool['pool']['id'], member)
                            new_member = gpm(
                                ctx, member['member']['id'])
                            upm = self.mock_api.update_member
                            upm.assert_called_once_with(
                                mock.ANY,
                                self._convert_unicode_dict(
                                    old_member.to_dict()),
                                self._convert_unicode_dict(
                                    new_member.to_dict()),
                                'host')

    def test_delete_member(self):
        ctx = context.get_admin_context()
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                with self.pool(listener_id=listener['listener']['id'],
                               no_delete=True) as pool:
                    self._update_status(models.Listener, constants.ACTIVE,
                                        listener['listener']['id'])
                    self._update_status(models.LoadBalancer, constants.ACTIVE,
                                        loadbalancer['loadbalancer']['id'])
                    self._update_status(models.PoolV2, constants.ACTIVE,
                                        pool['pool']['id'])
                    with self.subnet(cidr='11.0.0.0/24') as subnet:
                        with self.member(pool_id=pool['pool']['id'],
                                         subnet=subnet,
                                         no_delete=True) as member:
                            self._update_status(models.Listener,
                                                constants.ACTIVE,
                                                listener['listener']['id'])
                            self._update_status(models.LoadBalancer,
                                                constants.ACTIVE,
                                                loadbalancer[
                                                    'loadbalancer']['id'])
                            self._update_status(models.PoolV2,
                                                constants.ACTIVE,
                                                pool['pool']['id'])
                            self._update_status(models.MemberV2,
                                                constants.ACTIVE,
                                                member['member']['id'])

                            self.plugin_instance.delete_pool_member(
                                ctx,
                                member['member']['id'],
                                pool['pool']['id'])
                            nmember = self.plugin_instance.db.get_pool_member(
                                ctx, member['member']['id'])
                            dpm = self.mock_api.delete_member
                            dpm.assert_called_once_with(
                                mock.ANY,
                                self._convert_unicode_dict(nmember.to_dict()),
                                'host')

    def test_create_health_monitor(self):
        ctx = context.get_admin_context()
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                with self.pool(listener_id=listener['listener']['id'],
                               no_delete=True) as pool:
                    self._update_status(models.Listener, constants.ACTIVE,
                                        listener['listener']['id'])
                    self._update_status(models.LoadBalancer, constants.ACTIVE,
                                        loadbalancer['loadbalancer']['id'])
                    self._update_status(models.PoolV2, constants.ACTIVE,
                                        pool['pool']['id'])
                    with self.healthmonitor(pool_id=pool['pool']['id'],
                                            no_delete=True) as monitor:
                        self._update_status(models.HealthMonitorV2,
                                            constants.ACTIVE,
                                            monitor['healthmonitor']['id'])
                        nmonitor = self.plugin_instance.db.get_healthmonitor(
                            ctx, monitor['healthmonitor']['id']).to_dict()
                        pc = constants.PENDING_CREATE
                        nmonitor['provisioning_status'] = pc
                        pm = self.mock_api.create_health_monitor
                        pm.assert_called_once_with(
                            mock.ANY,
                            self._convert_unicode_dict(nmonitor),
                            'host'
                        )

    def test_update_health_monitor(self):
        ctx = context.get_admin_context()
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                with self.pool(listener_id=listener['listener']['id'],
                               no_delete=True) as pool:
                    self._update_status(models.Listener, constants.ACTIVE,
                                        listener['listener']['id'])
                    self._update_status(models.LoadBalancer, constants.ACTIVE,
                                        loadbalancer['loadbalancer']['id'])
                    self._update_status(models.PoolV2, constants.ACTIVE,
                                        pool['pool']['id'])
                    with self.healthmonitor(pool_id=pool['pool']['id'],
                                            no_delete=True) as monitor:
                        self._update_status(models.Listener,
                                            constants.ACTIVE,
                                            listener['listener']['id'])
                        self._update_status(models.LoadBalancer,
                                            constants.ACTIVE,
                                            loadbalancer[
                                                'loadbalancer']['id'])
                        self._update_status(models.PoolV2,
                                            constants.ACTIVE,
                                            pool['pool']['id'])
                        self._update_status(models.HealthMonitorV2,
                                            constants.ACTIVE,
                                            monitor['healthmonitor']['id'])
                        nmonitor = self.plugin_instance.db.get_healthmonitor(
                            ctx, monitor['healthmonitor']['id']).to_dict()
                        pc = constants.PENDING_CREATE
                        nmonitor['provisioning_status'] = pc
                        gpm = self.plugin_instance.db.get_healthmonitor
                        old_member = gpm(
                            ctx, monitor['healthmonitor']['id'])
                        self.plugin_instance.update_healthmonitor(
                            ctx,
                            monitor['healthmonitor']['id'],
                            monitor)
                        new_member = gpm(
                            ctx, monitor['healthmonitor']['id'])
                        upm = self.mock_api.update_health_monitor
                        upm.assert_called_once_with(
                            mock.ANY,
                            self._convert_unicode_dict(
                                old_member.to_dict()),
                            self._convert_unicode_dict(
                                new_member.to_dict()),
                            'host')

    def test_delete_health_monitor(self):
        ctx = context.get_admin_context()
        with self.loadbalancer(no_delete=True) as loadbalancer:
            self._update_status(models.LoadBalancer, constants.ACTIVE,
                                loadbalancer['loadbalancer']['id'])
            with self.listener(
                    loadbalancer_id=loadbalancer[
                        'loadbalancer']['id'], no_delete=True) as listener:
                self._update_status(models.Listener, constants.ACTIVE,
                                    listener['listener']['id'])
                self._update_status(models.LoadBalancer, constants.ACTIVE,
                                    loadbalancer['loadbalancer']['id'])
                with self.pool(listener_id=listener['listener']['id'],
                               no_delete=True) as pool:
                    self._update_status(models.Listener, constants.ACTIVE,
                                        listener['listener']['id'])
                    self._update_status(models.LoadBalancer, constants.ACTIVE,
                                        loadbalancer['loadbalancer']['id'])
                    self._update_status(models.PoolV2, constants.ACTIVE,
                                        pool['pool']['id'])
                    with self.healthmonitor(pool_id=pool['pool']['id'],
                                            no_delete=True) as monitor:
                        self._update_status(models.Listener,
                                            constants.ACTIVE,
                                            listener['listener']['id'])
                        self._update_status(models.LoadBalancer,
                                            constants.ACTIVE,
                                            loadbalancer[
                                                'loadbalancer']['id'])
                        self._update_status(models.PoolV2,
                                            constants.ACTIVE,
                                            pool['pool']['id'])
                        self._update_status(models.HealthMonitorV2,
                                            constants.ACTIVE,
                                            monitor['healthmonitor']['id'])

                        self.plugin_instance.delete_healthmonitor(
                            ctx,
                            monitor['healthmonitor']['id'])
                        nmonitor = self.plugin_instance.db.get_healthmonitor(
                            ctx, monitor['healthmonitor']['id'])
                        dpm = self.mock_api.delete_health_monitor
                        dpm.assert_called_once_with(
                            mock.ANY,
                            self._convert_unicode_dict(nmonitor.to_dict()),
                            'host')
