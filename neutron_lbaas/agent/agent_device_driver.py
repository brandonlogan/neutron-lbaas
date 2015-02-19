# Copyright 2013 OpenStack Foundation.  All rights reserved
# Copyright 2015 Rackspace
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

import abc

import six

from neutron_lbaas.services.loadbalancer.drivers import driver_base
from neutron_lbaas.services.loadbalancer.drivers import driver_mixins


@six.add_metaclass(abc.ABCMeta)
class AgentDeviceDriver(driver_base.LoadBalancerBaseDriver):
    """Abstract device driver that defines the API required by LBaaS agent."""

    def __init__(self, conf, plugin_rpc):
        self.conf = conf
        self.plugin_rpc = plugin_rpc

    @abc.abstractmethod
    def get_name(self):
        """Returns unique name across all LBaaS device drivers."""
        pass

    @abc.abstractmethod
    def deploy_instance(self, loadbalancer):
        """Fully deploys a loadbalancer instance from a given loadbalancer."""
        pass

    @abc.abstractmethod
    def undeploy_instance(self, loadbalancer_id, **kwargs):
        """Fully undeploys the loadbalancer instance."""
        pass

    def remove_orphans(self, known_loadbalancer_ids):
        # Not all drivers will support this
        raise NotImplementedError()


class BaseManagerMixin(driver_mixins.BaseManagerMixin):

    def db_delete_method(self):
        # We do not need to use this method
        raise NotImplementedError()

    def successful_completion(self, context, obj, delete=False):
        # We do not need to use this method
        raise NotImplementedError()

    def failed_completion(self, context, obj):
        # We do not need to use this method
        raise NotImplementedError()


class BaseLoadBalancerManager(BaseManagerMixin, driver_mixins.BaseStatsMixin,
                              driver_mixins.BaseRefreshMixin):
    pass


class BaseListenerManager(BaseManagerMixin):
    pass


class BasePoolManager(BaseManagerMixin):
    pass


class BaseMemberManager(BaseManagerMixin):
    pass


class BaseHealthMonitorManager(BaseManagerMixin):
    pass
