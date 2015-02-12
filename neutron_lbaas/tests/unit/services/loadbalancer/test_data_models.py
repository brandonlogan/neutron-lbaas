# Copyright 2015 Rackspace.
# All Rights Reserved.
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

from neutron_lbaas.services.loadbalancer import data_models
from neutron_lbaas.tests import base


class DataModelsTest(base.BaseTestCase):

    def setUp(self):
        super(DataModelsTest, self).setUp()

    def test_build_dict(self):
        sp = data_models.SessionPersistence(type='COOKIE',
                                            cookie_name='cwooky')
        hm = data_models.HealthMonitor(id='4231',
                                       tenant_id='2134',
                                       type='HTTP',
                                       delay='30',
                                       timeout='20',
                                       max_retries='3',
                                       http_method='POST',
                                       url_path='/path',
                                       expected_codes='404',
                                       provisioning_status='ACTIVE',
                                       admin_state_up='True')
        member = data_models.Member(id='2231',
                                    tenant_id='2134',
                                    address='10.0.0.7',
                                    protocol_port='80',
                                    weight='3',
                                    subnet_id='subnetid')
        pool = data_models.Pool(id='4114',
                                tenant_id='2134',
                                name='poolname',
                                description='imadescription',
                                healthmonitor_id='4231',
                                protocol='HTTP',
                                lb_algorithm='ROUND_ROBIN',
                                admin_state_up='True',
                                operating_status='ACTIVE',
                                provisioning_status='ACTIVE',
                                members=[member],
                                healthmonitor=hm,
                                sessionpersistence=sp)
        listener = data_models.Listener(id='11324',
                                        tenant_id='2134',
                                        name='listename',
                                        default_pool=pool)
        ipa = data_models.IPAllocation(ip_address='11.2.2.2',
                                       subnet_id='subnetid2',
                                       network_id='netid1')
        port = data_models.Port(id='14532',
                                tenant_id='2134',
                                fixed_ips=[ipa])
        lb = data_models.LoadBalancer(id='32214',
                                      tenant_id='2134',
                                      listeners=[listener],
                                      name='specialLB',
                                      vip_port=port)
        ret = data_models.build_dict(lb)
        self.assertEqual('specialLB', ret['loadbalancer']['name'])
        self.assertEqual('listename',
                         ret['loadbalancer'][
                             'listeners'][0]['listener']['name'])
        # TODO(ptoohill): more tests...
