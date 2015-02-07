# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import sys

import mock
import netaddr
import webob.exc

from neutron.agent import securitygroups_rpc as sg_cfg
from neutron.common import rpc as n_rpc
from neutron import context
from neutron.db import api as db_api
from neutron.db import model_base
from neutron import manager
from neutron.tests.unit.plugins.ml2.drivers.cisco.apic import (
    base as mocked)
from neutron.tests.unit.plugins.ml2 import test_plugin
from oslo_config import cfg

sys.modules["apicapi"] = mock.Mock()

from gbpservice.neutron.services.grouppolicy import (
    group_policy_context as p_context)
from gbpservice.neutron.services.grouppolicy import config
from gbpservice.neutron.services.grouppolicy.drivers.cisco.apic import (
    apic_mapping as amap)
from gbpservice.neutron.services.servicechain import config as sc_cfg
from gbpservice.neutron.tests.unit.services.grouppolicy import (
    test_grouppolicy_plugin as test_gp_plugin)


APIC_L2_POLICY = 'l2_policy'
APIC_L3_POLICY = 'l3_policy'
APIC_POLICY_RULE_SET = 'policy_rule_set'
APIC_POLICY_TARGET_GROUP = 'policy_target_group'
APIC_POLICY_RULE = 'policy_rule'

APIC_EXTERNAL_RID = '1.0.0.1'

AGENT_TYPE = 'Open vSwitch agent'
AGENT_CONF = {'alive': True, 'binary': 'somebinary',
              'topic': 'sometopic', 'agent_type': AGENT_TYPE}
SERVICECHAIN_SPECS = 'servicechain/servicechain_specs'
SERVICECHAIN_NODES = 'servicechain/servicechain_nodes'
SERVICECHAIN_INSTANCES = 'servicechain/servicechain_instances'


def echo(context, string, prefix=''):
    return prefix + string


class MockCallRecorder(mock.Mock):
    recorded_call_set = set()

    def __call__(self, *args, **kwargs):
        self.recorded_call_set.add(self.generate_entry(*args, **kwargs))
        return mock.Mock()

    def call_happened_with(self, *args, **kwargs):
        return self.generate_entry(*args, **kwargs) in self.recorded_call_set

    def generate_entry(self, *args, **kwargs):
        return args, tuple((x, kwargs[x]) for x in sorted(kwargs.keys()))


class ApicMappingTestCase(
        test_gp_plugin.GroupPolicyPluginTestCase,
        mocked.ControllerMixin, mocked.ConfigMixin):

    def setUp(self):
        cfg.CONF.register_opts(sg_cfg.security_group_opts, 'SECURITYGROUP')
        config.cfg.CONF.set_override('policy_drivers',
                                     ['implicit_policy', 'apic'],
                                     group='group_policy')
        sc_cfg.cfg.CONF.set_override('servicechain_drivers',
                                     ['dummy'],
                                     group='servicechain')
        config.cfg.CONF.set_override('enable_security_group', False,
                                     group='SECURITYGROUP')
        n_rpc.create_connection = mock.Mock()
        amap.ApicMappingDriver.get_apic_manager = mock.Mock()
        self.set_up_mocks()
        ml2_opts = {
            'mechanism_drivers': ['apic_gbp'],
            'type_drivers': ['opflex'],
            'tenant_network_types': ['opflex']
        }
        for opt, val in ml2_opts.items():
                cfg.CONF.set_override(opt, val, 'ml2')
        self.agent = {'configurations': {
            'opflex_networks': None,
            'bridge_mappings': {'physnet1': 'br-eth1'}},
            'alive': True}
        mock.patch('gbpservice.neutron.services.grouppolicy.drivers.cisco.'
                   'apic.apic_mapping.ApicMappingDriver._setup_rpc').start()
        host_agents = mock.patch('neutron.plugins.ml2.driver_context.'
                                 'PortContext.host_agents').start()
        host_agents.return_value = [self.agent]
        super(ApicMappingTestCase, self).setUp(
            core_plugin=test_plugin.PLUGIN_NAME)
        engine = db_api.get_engine()
        model_base.BASEV2.metadata.create_all(engine)
        plugin = manager.NeutronManager.get_plugin()
        plugin.remove_networks_from_down_agents = mock.Mock()
        plugin.is_agent_down = mock.Mock(return_value=False)
        self.driver = manager.NeutronManager.get_service_plugins()[
            'GROUP_POLICY'].policy_driver_manager.policy_drivers['apic'].obj
        amap.ApicMappingDriver.get_base_synchronizer = mock.Mock()
        self.driver.name_mapper = mock.Mock()
        self.driver.name_mapper.tenant = echo
        self.driver.name_mapper.l2_policy = echo
        self.driver.name_mapper.l3_policy = echo
        self.driver.name_mapper.policy_rule_set = echo
        self.driver.name_mapper.policy_rule = echo
        self.driver.name_mapper.app_profile.return_value = mocked.APIC_AP
        self.driver.name_mapper.policy_target_group = echo
        self.driver.name_mapper.external_policy = echo
        self.driver.name_mapper.external_segment = echo
        self.driver.apic_manager = mock.Mock(name_mapper=mock.Mock(),
                                             ext_net_dict={})
        self.driver.apic_manager.apic.transaction = self.fake_transaction
        self.driver.notifier = mock.Mock()
        amap.apic_manager.TENANT_COMMON = 'common'
        self.common_tenant = amap.apic_manager.TENANT_COMMON

    def _get_object(self, type, id, api):
        req = self.new_show_request(type, id, self.fmt)
        return self.deserialize(self.fmt, req.get_response(api))

    def _build_external_dict(self, name, cidr_exposed):
        return {name: {
                'switch': mocked.APIC_EXT_SWITCH,
                'port': mocked.APIC_EXT_MODULE + '/' + mocked.APIC_EXT_PORT,
                'encap': mocked.APIC_EXT_ENCAP,
                'router_id': APIC_EXTERNAL_RID,
                'cidr_exposed': cidr_exposed,
                'gateway_ip': str(netaddr.IPNetwork(cidr_exposed)[1])}}

    def _mock_external_dict(self, data):
        self.driver.apic_manager.ext_net_dict = {}
        for x in data:
            self.driver.apic_manager.ext_net_dict.update(
                self._build_external_dict(x[0], x[1]))

    def _check_call_list(self, expected, observed):
        for call in expected:
            self.assertTrue(call in observed,
                            msg='Call not found, expected:\n%s\nobserved:'
                                '\n%s' % (str(call), str(observed)))
            observed.remove(call)
        self.assertFalse(
            len(observed),
            msg='There are more calls than expected: %s' % str(observed))

    def _create_simple_policy_rule(self, direction='bi', protocol='tcp',
                                   port_range=80, shared=False,
                                   action_type='allow'):
        cls = self.create_policy_classifier(
            direction=direction, protocol=protocol,
            port_range=port_range, shared=shared)['policy_classifier']

        action = self.create_policy_action(
            action_type=action_type, shared=shared)['policy_action']
        return self.create_policy_rule(
            policy_classifier_id=cls['id'], policy_actions=[action['id']],
            shared=shared)['policy_rule']

    def _bind_port_to_host(self, port_id, host):
        data = {'port': {'binding:host_id': host}}
        # Create EP with bound port
        req = self.new_update_request('ports', data, port_id,
                                      self.fmt)
        return self.deserialize(self.fmt, req.get_response(self.api))


class TestPolicyTarget(ApicMappingTestCase):

    def test_policy_target_port_deleted_on_apic(self):
        ptg = self.create_policy_target_group()['policy_target_group']
        subnet = self._get_object('subnets', ptg['subnets'][0], self.api)
        with self.port(subnet=subnet) as port:
            self._bind_port_to_host(port['port']['id'], 'h1')
            pt = self.create_policy_target(
                policy_target_group_id=ptg['id'], port_id=port['port']['id'])
            self.new_delete_request(
                'policy_targets', pt['policy_target']['id'],
                self.fmt).get_response(self.ext_api)
            self.assertTrue(self.driver.notifier.port_update.called)

    def test_policy_target_delete_no_port(self):
        ptg = self.create_policy_target_group()['policy_target_group']
        subnet = self._get_object('subnets', ptg['subnets'][0], self.api)
        with self.port(subnet=subnet) as port:
            self._bind_port_to_host(port['port']['id'], 'h1')
            pt = self.create_policy_target(
                policy_target_group_id=ptg['id'], port_id=port['port']['id'])
            res = self.new_delete_request('ports', port['port']['id'],
                                          self.fmt).get_response(self.api)
            self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
            self.delete_policy_target(pt['policy_target']['id'],
                                      expected_res_status=204)

    def test_delete_policy_target_notification_no_apic_network(self):
        ptg = self.create_policy_target_group(
            name="ptg1")['policy_target_group']
        pt1 = self.create_policy_target(
            policy_target_group_id=ptg['id'])['policy_target']
        self._bind_port_to_host(pt1['port_id'], 'h1')
        # Implicit port will be deleted with the PT
        self.delete_policy_target(pt1['id'], expected_res_status=204)
        # No notification needed
        self.assertFalse(self.driver.notifier.port_update.called)
        self.driver.notifier.port_update.reset_mock()
        subnet = self._get_object('subnets', ptg['subnets'][0], self.api)
        with self.port(subnet=subnet) as port:
            # Create EP with bound port
            port = self._bind_port_to_host(port['port']['id'], 'h1')
            pt1 = self.create_policy_target(
                policy_target_group_id=ptg['id'], port_id=port['port']['id'])
            # Explicit port won't be deleted with PT
            self.delete_policy_target(pt1['policy_target']['id'],
                                      expected_res_status=204)
            # Issue notification for the agent
            self.assertTrue(self.driver.notifier.port_update.called)

    def test_get_gbp_details(self):
        ptg = self.create_policy_target_group(
            name="ptg1")['policy_target_group']
        pt1 = self.create_policy_target(
            policy_target_group_id=ptg['id'])['policy_target']
        self._bind_port_to_host(pt1['port_id'], 'h1')
        pt2 = self.create_policy_target(
            policy_target_group_id=ptg['id'])['policy_target']
        self._bind_port_to_host(pt2['port_id'], 'h1')

        # Delete EP1
        self.new_delete_request('policy_targets', pt1['id'],
                                self.fmt).get_response(self.ext_api)
        # APIC path not deleted
        mgr = self.driver.apic_manager
        self.assertEqual(mgr.ensure_path_deleted_for_port.call_count, 0)
        mapping = self.driver.get_gbp_details(context.get_admin_context(),
            device='tap%s' % pt1['port_id'], host='h1')
        self.assertEqual(pt1['port_id'], mapping['port_id'])
        self.assertEqual(ptg['id'], mapping['ptg_id'])
        self.assertEqual(ptg['id'], mapping['endpoint_group_name'])

    def _bind_port_to_host(self, port_id, host):
        plugin = manager.NeutronManager.get_plugin()
        ctx = context.get_admin_context()
        agent = {'host': host}
        agent.update(AGENT_CONF)
        plugin.create_or_update_agent(ctx, agent)
        data = {'port': {'binding:host_id': host}}
        # Create EP with bound port
        req = self.new_update_request('ports', data, port_id,
                                      self.fmt)
        return self.deserialize(self.fmt, req.get_response(self.api))

    def test_network_port_bound_to_ptg(self):
        ptg = self.create_policy_target_group()['policy_target_group']
        subnet = self._get_object('subnets', ptg['subnets'][0], self.api)
        with self.port(subnet=subnet, device_owner='some-owner') as port:
            # This will have created 2 ports. The one stored in port and an
            # Implicit DHCP port. Verify that both exist and are associated
            # to a PT
            pts = self._list(
                'policy_targets', query_params='policy_target_group_id=' +
                                               ptg['id'])['policy_targets']
            self.assertEqual(1, len(pts))
            self.assertEqual(pts[0]['port_id'], port['port']['id'])

    def test_get_gbp_details_shadow(self):
        l2p = self.create_l2_policy()['l2_policy']
        network = self._get_object('networks', l2p['network_id'], self.api)
        with self.subnet(network=network) as sub:
            with self.port(subnet=sub) as port:
                self._bind_port_to_host(port['port']['id'], 'h1')
                mapping = self.driver.get_gbp_details(
                    context.get_admin_context(),
                    device='tap%s' % port['port']['id'], host='h1')
                self.assertEqual(port['port']['id'], mapping['port_id'])
                self.assertEqual(amap.SHADOW_PREFIX + l2p['id'],
                                 mapping['endpoint_group_name'])

    def test_explicit_port(self):
        with self.network() as net:
            with self.subnet(network=net) as sub:
                with self.port(subnet=sub) as port:
                    self._bind_port_to_host(port['port']['id'], 'h1')
                    l2p = self.create_l2_policy(
                        network_id=net['network']['id'])['l2_policy']
                    ptg = self.create_policy_target_group(
                        l2_policy_id=l2p['id'])['policy_target_group']
                    self.create_policy_target(
                        port_id=port['port']['id'],
                        policy_target_group_id=ptg['id'])
                    self.assertTrue(self.driver.notifier.port_update.called)


class TestPolicyTargetGroup(ApicMappingTestCase):

    def _test_policy_target_group_created_on_apic(self, shared=False):
        ptg = self.create_policy_target_group(
            name="ptg1", shared=shared)['policy_target_group']
        tenant = self.common_tenant if shared else ptg['tenant_id']
        mgr = self.driver.apic_manager
        expected_calls = [
            mock.call(tenant, ptg['id'], bd_name=ptg['l2_policy_id'],
                      bd_owner=tenant),
            mock.call(tenant, amap.SHADOW_PREFIX + ptg['l2_policy_id'],
                      bd_name=ptg['l2_policy_id'], bd_owner=tenant,
                      transaction=mock.ANY)]
        self._check_call_list(
            expected_calls, mgr.ensure_epg_created.call_args_list)

    def test_policy_target_group_created_on_apic(self):
        self._test_policy_target_group_created_on_apic()

    def test_policy_target_group_created_on_apic_shared(self):
        self._test_policy_target_group_created_on_apic(shared=True)

    def _test_ptg_policy_rule_set_created(self, provider=True, shared=False):
        cntr = self.create_policy_rule_set(name='c',
                                           shared=shared)['policy_rule_set']
        l2p = self.create_l2_policy()['l2_policy']
        mgr = self.driver.apic_manager
        mgr.set_contract_for_epg.reset_mock()
        if provider:
            ptg = self.create_policy_target_group(
                l2_policy_id=l2p['id'],
                provided_policy_rule_sets={cntr['id']: 'scope'})[
                    'policy_target_group']
        else:
            ptg = self.create_policy_target_group(
                l2_policy_id=l2p['id'],
                consumed_policy_rule_sets={cntr['id']: 'scope'})[
                    'policy_target_group']

        # Verify that the apic call is issued
        ct_owner = self.common_tenant if shared else cntr['tenant_id']
        expected_calls = [
            mock.call(
                ptg['tenant_id'], ptg['id'], cntr['id'],
                transaction='transaction', contract_owner=ct_owner,
                provider=provider),
            mock.call(
                ptg['tenant_id'], ptg['id'],
                amap.SERVICE_PREFIX + ptg['l2_policy_id'],
                transaction='transaction', contract_owner=ptg['tenant_id'],
                provider=False)]
        self._check_call_list(expected_calls,
                              mgr.set_contract_for_epg.call_args_list)

    def _test_ptg_policy_rule_set_updated(self, provider=True, shared=False):
        p_or_c = {True: 'provided_policy_rule_sets',
                  False: 'consumed_policy_rule_sets'}
        cntr = self.create_policy_rule_set(
            name='c1', shared=shared)['policy_rule_set']
        new_cntr = self.create_policy_rule_set(
            name='c2', shared=shared)['policy_rule_set']

        if provider:
            ptg = self.create_policy_target_group(
                provided_policy_rule_sets={cntr['id']: 'scope'})
        else:
            ptg = self.create_policy_target_group(
                consumed_policy_rule_sets={cntr['id']: 'scope'})

        data = {'policy_target_group': {p_or_c[provider]:
                {new_cntr['id']: 'scope'}}}
        req = self.new_update_request('policy_target_groups', data,
                                      ptg['policy_target_group']['id'],
                                      self.fmt)
        ptg = self.deserialize(self.fmt, req.get_response(self.ext_api))
        ptg = ptg['policy_target_group']
        mgr = self.driver.apic_manager
        ct_owner = self.common_tenant if shared else cntr['tenant_id']
        mgr.set_contract_for_epg.assert_called_with(
            ptg['tenant_id'], ptg['id'], new_cntr['id'],
            contract_owner=ct_owner, transaction='transaction',
            provider=provider)
        mgr.unset_contract_for_epg.assert_called_with(
            ptg['tenant_id'], ptg['id'], cntr['id'],
            contract_owner=ct_owner,
            transaction='transaction', provider=provider)

    def test_ptg_policy_rule_set_provider_created(self):
        self._test_ptg_policy_rule_set_created()

    def test_ptg_policy_rule_set_provider_updated(self):
        self._test_ptg_policy_rule_set_updated()

    def test_ptg_policy_rule_set_consumer_created(self):
        self._test_ptg_policy_rule_set_created(False)

    def test_ptg_policy_rule_set_consumer_updated(self):
        self._test_ptg_policy_rule_set_updated(False)

    def test_ptg_policy_rule_set_provider_created_shared(self):
        self._test_ptg_policy_rule_set_created(shared=True)

    def test_ptg_policy_rule_set_provider_updated_shared(self):
        self._test_ptg_policy_rule_set_updated(shared=True)

    def test_ptg_policy_rule_set_consumer_created_shared(self):
        self._test_ptg_policy_rule_set_created(False, shared=True)

    def test_ptg_policy_rule_set_consumer_updated_shared(self):
        self._test_ptg_policy_rule_set_updated(False, shared=True)

    def _test_policy_target_group_deleted_on_apic(self, shared=False):
        ptg = self.create_policy_target_group(
            name="ptg1", shared=shared)['policy_target_group']
        req = self.new_delete_request('policy_target_groups',
                                      ptg['id'], self.fmt)
        req.get_response(self.ext_api)
        mgr = self.driver.apic_manager
        tenant = self.common_tenant if shared else ptg['tenant_id']

        expected_calls = [
            mock.call(tenant, ptg['id']),
            mock.call(tenant, amap.SHADOW_PREFIX + ptg['l2_policy_id'],
                      transaction=mock.ANY)]
        self._check_call_list(expected_calls,
                              mgr.delete_epg_for_network.call_args_list)

    def test_policy_target_group_deleted_on_apic(self):
        self._test_policy_target_group_deleted_on_apic()

    def test_policy_target_group_deleted_on_apic_shared(self):
        self._test_policy_target_group_deleted_on_apic(shared=True)

    def _test_policy_target_group_subnet_created_on_apic(self, shared=False):

        ptg = self._create_explicit_subnet_ptg('10.0.0.0/24', shared=shared)

        mgr = self.driver.apic_manager
        tenant = self.common_tenant if shared else ptg['tenant_id']
        mgr.ensure_subnet_created_on_apic.assert_called_once_with(
            tenant, ptg['l2_policy_id'], '10.0.0.1/24',
            transaction='transaction')

    def test_policy_target_group_subnet_created_on_apic(self):
        self._test_policy_target_group_subnet_created_on_apic()

    def test_policy_target_group_subnet_created_on_apic_shared(self):
        self._test_policy_target_group_subnet_created_on_apic(shared=True)

    def _test_policy_target_group_subnet_added(self, shared=False):
        ptg = self._create_explicit_subnet_ptg('10.0.0.0/24', shared=shared)
        l2p = self._get_object('l2_policies', ptg['l2_policy_id'],
                               self.ext_api)
        network = self._get_object('networks', l2p['l2_policy']['network_id'],
                                   self.api)

        with self.subnet(network=network, cidr='10.0.1.0/24') as subnet:
            data = {'policy_target_group':
                    {'subnets': ptg['subnets'] + [subnet['subnet']['id']]}}
            mgr = self.driver.apic_manager
            self.new_update_request('policy_target_groups', data, ptg['id'],
                                    self.fmt).get_response(self.ext_api)
            tenant = self.common_tenant if shared else ptg['tenant_id']
            mgr.ensure_subnet_created_on_apic.assert_called_with(
                tenant, ptg['l2_policy_id'], '10.0.1.1/24',
                transaction='transaction')

    def test_policy_target_group_subnet_added(self):
        self._test_policy_target_group_subnet_added()

    def test_policy_target_group_subnet_added_shared(self):
        self._test_policy_target_group_subnet_added(shared=True)

    def _test_process_subnet_update(self, shared=False):
        ptg = self._create_explicit_subnet_ptg('10.0.0.0/24', shared=shared)
        subnet = self._get_object('subnets', ptg['subnets'][0], self.api)
        subnet2 = copy.deepcopy(subnet)
        subnet2['subnet']['gateway_ip'] = '10.0.0.254'
        mgr = self.driver.apic_manager
        mgr.reset_mock()
        self.driver.process_subnet_changed(context.get_admin_context(),
                                           subnet['subnet'], subnet2['subnet'])

        tenant = self.common_tenant if shared else ptg['tenant_id']
        mgr.ensure_subnet_created_on_apic.assert_called_once_with(
            tenant, ptg['l2_policy_id'], '10.0.0.254/24',
            transaction='transaction')
        mgr.ensure_subnet_deleted_on_apic.assert_called_with(
            tenant, ptg['l2_policy_id'], '10.0.0.1/24',
            transaction='transaction')

    def test_process_subnet_update(self):
        self._test_process_subnet_update()

    def test_process_subnet_update_shared(self):
        self._test_process_subnet_update(shared=True)

    def test_multiple_ptg_per_l2p(self):
        l2p = self.create_l2_policy()['l2_policy']
        # Create first PTG
        ptg1 = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']
        ptg2 = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']
        self.assertEqual(ptg1['subnets'], ptg2['subnets'])

    def test_force_add_subnet(self):
        l2p = self.create_l2_policy()['l2_policy']
        # Create first PTG
        ptg1 = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']
        ptg2 = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']
        ctx = p_context.PolicyTargetGroupContext(
            self.driver.gbp_plugin, context.get_admin_context(), ptg2)
        # Emulate force add
        self.driver._use_implicit_subnet(ctx, force_add=True)
        # There now a new subnet, and it's added to both the PTGs
        self.assertEqual(2, len(ctx.current['subnets']))
        ptg1 = self.show_policy_target_group(ptg1['id'])['policy_target_group']
        self.assertEqual(2, len(ptg1['subnets']))
        ptg2 = self.show_policy_target_group(ptg2['id'])['policy_target_group']
        self.assertEqual(2, len(ptg2['subnets']))
        self.assertEqual(set(ptg1['subnets']), set(ptg2['subnets']))
        self.assertNotEqual(ptg2['subnets'][0], ptg2['subnets'][1])

    def _create_explicit_subnet_ptg(self, cidr, shared=False):
        l2p = self.create_l2_policy(name="l2p", shared=shared)
        l2p_id = l2p['l2_policy']['id']
        network_id = l2p['l2_policy']['network_id']
        network = self._get_object('networks', network_id, self.api)
        with self.subnet(network=network, cidr=cidr):
            # The subnet creation in the proper network causes the subnet ID
            # to be added to the PTG
            return self.create_policy_target_group(
                name="ptg1", l2_policy_id=l2p_id,
                shared=shared)['policy_target_group']


class TestL2Policy(ApicMappingTestCase):

    def _test_l2_policy_created_on_apic(self, shared=False):
        l2p = self.create_l2_policy(name="l2p", shared=shared)['l2_policy']

        tenant = self.common_tenant if shared else l2p['tenant_id']
        mgr = self.driver.apic_manager
        mgr.ensure_bd_created_on_apic.assert_called_once_with(
            tenant, l2p['id'], ctx_owner=tenant, ctx_name=l2p['l3_policy_id'],
            allow_broadcast=False, transaction=mock.ANY)
        mgr.ensure_epg_created.assert_called_once_with(
            tenant, amap.SHADOW_PREFIX + l2p['id'], bd_owner=tenant,
            bd_name=l2p['id'], transaction=mock.ANY)

    def test_l2_policy_created_on_apic(self):
        self._test_l2_policy_created_on_apic()

    def test_l2_policy_created_on_apic_shared(self):
        self._test_l2_policy_created_on_apic(shared=True)

    def _test_l2_policy_deleted_on_apic(self, shared=False):
        l2p = self.create_l2_policy(name="l2p", shared=shared)['l2_policy']
        req = self.new_delete_request('l2_policies', l2p['id'], self.fmt)
        req.get_response(self.ext_api)
        tenant = self.common_tenant if shared else l2p['tenant_id']
        mgr = self.driver.apic_manager
        mgr.delete_bd_on_apic.assert_called_once_with(
            tenant, l2p['id'], transaction=mock.ANY)
        mgr.delete_epg_for_network(tenant, amap.SHADOW_PREFIX + l2p['id'],
                                   transaction=mock.ANY)

    def test_l2_policy_deleted_on_apic(self):
        self._test_l2_policy_deleted_on_apic()

    def test_l2_policy_deleted_on_apic_shared(self):
        self._test_l2_policy_deleted_on_apic(shared=True)

    def test_pre_existing_subnets_added(self):
        with self.network() as net:
            with self.subnet(network=net) as sub:
                sub = sub['subnet']
                l2p = self.create_l2_policy(
                    network_id=net['network']['id'])['l2_policy']
                mgr = self.driver.apic_manager
                mgr.ensure_subnet_created_on_apic.assert_called_with(
                    l2p['tenant_id'], l2p['id'],
                    sub['gateway_ip'] + '/' + sub['cidr'].split('/')[1],
                    transaction=mock.ANY)
                ptg = self.create_policy_target_group(
                    l2_policy_id=l2p['id'])['policy_target_group']
                self.assertEqual(ptg['subnets'], [sub['id']])

    def test_l2_policy_updated(self):
        l2p = self.create_l2_policy()['l2_policy']
        mgr = self.driver.apic_manager
        mgr.ensure_bd_created_on_apic.reset_mock()
        self.update_l2_policy(l2p['id'], allow_broadcast=True)
        l2p = self.show_l2_policy(l2p['id'])['l2_policy']
        self.assertTrue(l2p['allow_broadcast'])
        mgr.ensure_bd_created_on_apic.assert_called_once_with(
            l2p['tenant_id'], l2p['id'], ctx_name=None,
            allow_broadcast=True)


class TestL3Policy(ApicMappingTestCase):

    def _test_l3_policy_created_on_apic(self, shared=False):
        l3p = self.create_l3_policy(name="l3p", shared=shared)['l3_policy']

        tenant = self.common_tenant if shared else l3p['tenant_id']
        mgr = self.driver.apic_manager
        mgr.ensure_context_enforced.assert_called_once_with(
            tenant, l3p['id'])

    def test_l3_policy_created_on_apic(self):
        self._test_l3_policy_created_on_apic()

    def test_l3_policy_created_on_apic_shared(self):
        self._test_l3_policy_created_on_apic(shared=True)

    def _test_l3_policy_deleted_on_apic(self, shared=False):
        l3p = self.create_l3_policy(name="l3p", shared=shared)['l3_policy']
        req = self.new_delete_request('l3_policies', l3p['id'], self.fmt)
        req.get_response(self.ext_api)

        tenant = self.common_tenant if shared else l3p['tenant_id']
        mgr = self.driver.apic_manager
        mgr.ensure_context_deleted.assert_called_once_with(
            tenant, l3p['id'])

    def test_l3_policy_deleted_on_apic(self):
        self._test_l3_policy_deleted_on_apic()

    def test_l3_policy_deleted_on_apic_shared(self):
        self._test_l3_policy_deleted_on_apic(shared=True)

    def _test_one_l3_policy_per_es(self, shared_es=False):
        # Verify 2 L3P created on same ES fails
        es = self.create_external_segment(
            cidr='192.168.0.0/24', shared=shared_es)['external_segment']
        self.create_l3_policy(external_segments={es['id']: ['192.168.0.1']},
                              expected_res_status=201)
        res = self.create_l3_policy(
            external_segments={es['id']: ['192.168.0.2']},
            expected_res_status=400)
        self.assertEqual('OnlyOneL3PolicyIsAllowedPerExternalSegment',
                         res['NeutronError']['type'])
        # Verify existing L3P updated to use used ES fails
        sneaky_l3p = self.create_l3_policy()['l3_policy']
        res = self.update_l3_policy(
            sneaky_l3p['id'], expected_res_status=400,
            external_segments={es['id']: ['192.168.0.3']})
        self.assertEqual('OnlyOneL3PolicyIsAllowedPerExternalSegment',
                         res['NeutronError']['type'])

    def test_one_l3_policy_per_es(self):
        self._test_one_l3_policy_per_es(shared_es=False)

    def test_one_l3_policy_per_es_shared(self):
        self._test_one_l3_policy_per_es(shared_es=True)

    def test_one_l3_policy_ip_on_es(self):
        # Verify L3P created with more than 1 IP on ES fails
        es = self.create_external_segment(
            cidr='192.168.0.0/24')['external_segment']
        res = self.create_l3_policy(
            external_segments={es['id']: ['192.168.0.2', '192.168.0.3']},
            expected_res_status=400)
        self.assertEqual('OnlyOneAddressIsAllowedPerExternalSegment',
                         res['NeutronError']['type'])
        # Verify L3P updated to more than 1 IP on ES fails
        sneaky_l3p = self.create_l3_policy(
            external_segments={es['id']: ['192.168.0.2']},
            expected_res_status=201)['l3_policy']
        res = self.update_l3_policy(
            sneaky_l3p['id'], expected_res_status=400,
            external_segments={es['id']: ['192.168.0.2', '192.168.0.3']})
        self.assertEqual('OnlyOneAddressIsAllowedPerExternalSegment',
                         res['NeutronError']['type'])

    def _test_l3p_plugged_to_es_at_creation(self, shared_es, shared_l3p):
        # Verify L3P is correctly plugged to ES on APIC during create
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es = self.create_external_segment(
            name='supported', cidr='192.168.0.0/24',
            shared=shared_es,
            external_routes=[{'destination': '0.0.0.0/0',
                              'nexthop': '192.168.0.254'},
                             {'destination': '128.0.0.0/16',
                              'nexthop': None}])['external_segment']

        # Create with explicit address
        l3p = self.create_l3_policy(
            shared=shared_l3p,
            tenant_id=es['tenant_id'] if not shared_es else 'another_tenant',
            external_segments={es['id']: ['192.168.0.3']},
            expected_res_status=201)['l3_policy']

        owner = self.common_tenant if shared_es else es['tenant_id']
        mgr = self.driver.apic_manager
        mgr.ensure_external_routed_network_created.assert_called_once_with(
            es['id'], owner=owner, context=l3p['id'],
            transaction=mock.ANY)
        mgr.ensure_logical_node_profile_created.assert_called_once_with(
            es['id'], mocked.APIC_EXT_SWITCH, mocked.APIC_EXT_MODULE,
            mocked.APIC_EXT_PORT, mocked.APIC_EXT_ENCAP, '192.168.0.3',
            owner=owner, router_id=APIC_EXTERNAL_RID,
            transaction=mock.ANY)

        expected_route_calls = [
            mock.call(es['id'], mocked.APIC_EXT_SWITCH, '192.168.0.254',
                      owner=owner, subnet='0.0.0.0/0',
                      transaction=mock.ANY),
            mock.call(es['id'], mocked.APIC_EXT_SWITCH, '192.168.0.1',
                      owner=owner, subnet='128.0.0.0/16',
                      transaction=mock.ANY)]
        self._check_call_list(expected_route_calls,
                              mgr.ensure_static_route_created.call_args_list)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_l3p_plugged_to_es_at_creation_1(self):
        self._test_l3p_plugged_to_es_at_creation(shared_es=True,
                                                 shared_l3p=False)

    def test_l3p_plugged_to_es_at_creation_2(self):
        self._test_l3p_plugged_to_es_at_creation(shared_es=True,
                                                 shared_l3p=True)

    def test_l3p_plugged_to_es_at_creation_3(self):
        self._test_l3p_plugged_to_es_at_creation(shared_es=False,
                                                 shared_l3p=False)

    def _test_l3p_plugged_to_es_at_update(self, shared_es, shared_l3p):
        # Verify L3P is correctly plugged to ES on APIC during update
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es = self.create_external_segment(
            name='supported', cidr='192.168.0.0/24',
            shared=shared_es,
            external_routes=[{'destination': '0.0.0.0/0',
                              'nexthop': '192.168.0.254'},
                             {'destination': '128.0.0.0/16',
                              'nexthop': None}])['external_segment']

        # Create with explicit address
        l3p = self.create_l3_policy(
            expected_res_status=201,
            tenant_id=es['tenant_id'] if not shared_es else 'another_tenant',
            shared=shared_l3p)['l3_policy']
        l3p = self.update_l3_policy(
            l3p['id'], tenant_id=l3p['tenant_id'], expected_res_status=200,
            external_segments={es['id']: ['192.168.0.3']})['l3_policy']

        mgr = self.driver.apic_manager
        owner = self.common_tenant if shared_es else es['tenant_id']
        mgr.ensure_external_routed_network_created.assert_called_once_with(
            es['id'], owner=owner, context=l3p['id'],
            transaction=mock.ANY)
        mgr.ensure_logical_node_profile_created.assert_called_once_with(
            es['id'], mocked.APIC_EXT_SWITCH, mocked.APIC_EXT_MODULE,
            mocked.APIC_EXT_PORT, mocked.APIC_EXT_ENCAP, '192.168.0.3',
            owner=owner, router_id=APIC_EXTERNAL_RID,
            transaction=mock.ANY)

        expected_route_calls = [
            mock.call(es['id'], mocked.APIC_EXT_SWITCH, '192.168.0.254',
                      owner=owner, subnet='0.0.0.0/0',
                      transaction=mock.ANY),
            mock.call(es['id'], mocked.APIC_EXT_SWITCH, '192.168.0.1',
                      owner=owner, subnet='128.0.0.0/16',
                      transaction=mock.ANY)]
        self._check_call_list(expected_route_calls,
                              mgr.ensure_static_route_created.call_args_list)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_l3p_plugged_to_es_at_update_1(self):
        self._test_l3p_plugged_to_es_at_update(shared_es=True,
                                               shared_l3p=False)

    def test_l3p_plugged_to_es_at_update_2(self):
        self._test_l3p_plugged_to_es_at_update(shared_es=True,
                                               shared_l3p=True)

    def test_l3p_plugged_to_es_at_update_3(self):
        self._test_l3p_plugged_to_es_at_update(shared_es=False,
                                               shared_l3p=False)

    def _test_l3p_unplugged_from_es_on_delete(self, shared_es, shared_l3p):
        self._mock_external_dict([('supported1', '192.168.0.2/24'),
                                 ('supported2', '192.168.1.2/24')])
        es1 = self.create_external_segment(
            name='supported1', cidr='192.168.0.0/24', shared=shared_es,
            external_routes=[{'destination': '0.0.0.0/0',
                              'nexthop': '192.168.0.254'},
                             {'destination': '128.0.0.0/16',
                              'nexthop': None}])['external_segment']
        es2 = self.create_external_segment(
            shared=shared_es, name='supported2',
            cidr='192.168.1.0/24')['external_segment']

        l3p = self.create_l3_policy(
            external_segments={es1['id']: ['192.168.0.3']}, shared=shared_l3p,
            tenant_id=es1['tenant_id'] if not shared_es else 'another_tenant',
            expected_res_status=201)['l3_policy']
        req = self.new_delete_request('l3_policies', l3p['id'], self.fmt)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)

        mgr = self.driver.apic_manager
        owner = self.common_tenant if shared_es else es1['tenant_id']
        mgr.delete_external_routed_network.assert_called_once_with(
            es1['id'], owner=owner)

        mgr.delete_external_routed_network.reset_mock()
        # Verify correct deletion for 2 ESs
        l3p = self.create_l3_policy(
            shared=shared_l3p,
            tenant_id=es1['tenant_id'] if not shared_es else 'another_tenant',
            external_segments={es1['id']: ['192.168.0.3'],
                               es2['id']: ['192.168.1.3']},
            expected_res_status=201)['l3_policy']
        req = self.new_delete_request('l3_policies', l3p['id'], self.fmt)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)

        expected_delete_calls = [
            mock.call(es1['id'], owner=owner),
            mock.call(es2['id'], owner=owner)]
        self._check_call_list(
            expected_delete_calls,
            mgr.delete_external_routed_network.call_args_list)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_l3p_unplugged_from_es_on_delete_1(self):
        self._test_l3p_unplugged_from_es_on_delete(shared_es=True,
                                                   shared_l3p=False)

    def test_l3p_unplugged_from_es_on_delete_2(self):
        self._test_l3p_unplugged_from_es_on_delete(shared_es=True,
                                                   shared_l3p=True)

    def test_l3p_unplugged_from_es_on_delete_3(self):
        self._test_l3p_unplugged_from_es_on_delete(shared_es=False,
                                                   shared_l3p=False)

    def _test_l3p_unplugged_from_es_on_update(self, shared_es, shared_l3p):
        self._mock_external_dict([('supported1', '192.168.0.2/24'),
                                 ('supported2', '192.168.1.2/24')])
        es1 = self.create_external_segment(
            name='supported1', cidr='192.168.0.0/24', shared=shared_es,
            external_routes=[{'destination': '0.0.0.0/0',
                              'nexthop': '192.168.0.254'},
                             {'destination': '128.0.0.0/16',
                              'nexthop': None}])['external_segment']
        es2 = self.create_external_segment(
            shared=shared_es,
            name='supported2', cidr='192.168.1.0/24')['external_segment']
        l3p = self.create_l3_policy(
            tenant_id=es1['tenant_id'] if not shared_es else 'another_tenant',
            shared=shared_l3p,
            external_segments={es1['id']: ['192.168.0.3']},
            expected_res_status=201)['l3_policy']

        mgr = self.driver.apic_manager
        owner = self.common_tenant if shared_es else es1['tenant_id']
        mgr.ensure_external_routed_network_created.reset_mock()
        mgr.ensure_logical_node_profile_created.reset_mock()
        mgr.ensure_static_route_created.reset_mock()

        l3p = self.update_l3_policy(
            l3p['id'], tenant_id=l3p['tenant_id'], expected_res_status=200,
            external_segments={es2['id']: ['192.168.1.3']})['l3_policy']

        mgr.delete_external_routed_network.assert_called_once_with(
            es1['id'], owner=owner)
        mgr.ensure_external_routed_network_created.assert_called_once_with(
            es2['id'], owner=owner, context=l3p['id'],
            transaction=mock.ANY)
        mgr.ensure_logical_node_profile_created.assert_called_once_with(
            es2['id'], mocked.APIC_EXT_SWITCH, mocked.APIC_EXT_MODULE,
            mocked.APIC_EXT_PORT, mocked.APIC_EXT_ENCAP, '192.168.1.3',
            owner=owner, router_id=APIC_EXTERNAL_RID,
            transaction=mock.ANY)
        self.assertFalse(mgr.ensure_static_route_created.called)

        mgr.delete_external_routed_network.reset_mock()
        self.update_l3_policy(
            l3p['id'], expected_res_status=200, tenant_id=l3p['tenant_id'],
            external_segments={es1['id']: ['192.168.0.3'],
                               es2['id']: ['192.168.1.3']})
        self.update_l3_policy(
            l3p['id'], tenant_id=l3p['tenant_id'],
            expected_res_status=200, external_segments={})
        expected_delete_calls = [
            mock.call(es1['id'], owner=owner),
            mock.call(es2['id'], owner=owner)]
        self._check_call_list(
            expected_delete_calls,
            mgr.delete_external_routed_network.call_args_list)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_l3p_unplugged_from_es_on_update_1(self):
        self._test_l3p_unplugged_from_es_on_update(shared_es=True,
                                                   shared_l3p=False)

    def test_l3p_unplugged_from_es_on_update_2(self):
        self._test_l3p_unplugged_from_es_on_update(shared_es=True,
                                                   shared_l3p=True)

    def test_l3p_unplugged_from_es_on_update_3(self):
        self._test_l3p_unplugged_from_es_on_update(shared_es=False,
                                                   shared_l3p=False)

    def test_verify_unsupported_es_noop(self):
        # Verify L3P is correctly plugged to ES on APIC during update
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es = self.create_external_segment(
            name='unsupported', cidr='192.168.0.0/24')['external_segment']
        self.create_l3_policy(
            external_segments={es['id']: ['192.168.0.3']},
            expected_res_status=201)

        mgr = self.driver.apic_manager
        self.assertFalse(mgr.ensure_external_routed_network_created.called)
        self.assertFalse(mgr.ensure_logical_node_profile_created.called)
        self.assertFalse(mgr.ensure_static_route_created.called)

    def test_cidr_exposd(self):
        # Verify "cidr_exposed" configuration is assigned to L3P when no
        # explicit address is configured
        self._mock_external_dict([('supported1', '192.168.0.2/24'),
                                  ('supported2', '192.168.1.2/24')])
        es1 = self.create_external_segment(
            name='supported1', cidr='192.168.0.0/24')['external_segment']
        es2 = self.create_external_segment(
            name='supported2', cidr='192.168.1.0/24')['external_segment']
        l3p = self.create_l3_policy(
            external_segments={es1['id']: []},
            expected_res_status=201)['l3_policy']
        self.assertEqual(['192.168.0.2'], l3p['external_segments'][es1['id']])

        l3p = self.update_l3_policy(
            l3p['id'], expected_res_status=200,
            external_segments={es1['id']: [], es2['id']: []})['l3_policy']
        self.assertEqual(['192.168.0.2'], l3p['external_segments'][es1['id']])
        self.assertEqual(['192.168.1.2'], l3p['external_segments'][es2['id']])

        # Address IP changed
        l3p = self.update_l3_policy(
            l3p['id'], expected_res_status=200,
            external_segments={es1['id']: ['192.168.0.3'],
                               es2['id']: []})['l3_policy']
        self.assertEqual(['192.168.0.3'], l3p['external_segments'][es1['id']])
        self.assertEqual(['192.168.1.2'], l3p['external_segments'][es2['id']])


class TestPolicyRuleSet(ApicMappingTestCase):

    # TODO(ivar): verify rule intersection with hierarchical PRS happens
    # on APIC
    def _test_policy_rule_set_created_on_apic(self, shared=False):
        ct = self.create_policy_rule_set(name="ctr",
                                         shared=shared)['policy_rule_set']

        tenant = self.common_tenant if shared else ct['tenant_id']
        mgr = self.driver.apic_manager
        mgr.create_contract.assert_called_once_with(
            ct['id'], owner=tenant, transaction='transaction')

    def test_policy_rule_set_created_on_apic(self):
        self._test_policy_rule_set_created_on_apic()

    def test_policy_rule_set_created_on_apic_shared(self):
        self._test_policy_rule_set_created_on_apic(shared=True)

    def _test_policy_rule_set_created_with_rules(self, shared=False):
        bi, in_d, out = range(3)
        rules = self._create_3_direction_rules(shared=shared)
        # exclude BI rule for now
        ctr = self.create_policy_rule_set(
            name="ctr", policy_rules=[x['id'] for x in rules[1:]])[
                'policy_rule_set']

        rule_owner = self.common_tenant if shared else rules[0]['tenant_id']
        # Verify that the in-out rules are correctly enforced on the APIC
        mgr = self.driver.apic_manager
        expected_calls = [
            mock.call(ctr['id'], ctr['id'], rules[in_d]['id'],
                      owner=ctr['tenant_id'], transaction='transaction',
                      unset=False, rule_owner=rule_owner),
            mock.call(ctr['id'], ctr['id'],
                      amap.REVERSE_PREFIX + rules[out]['id'],
                      owner=ctr['tenant_id'], transaction='transaction',
                      unset=False, rule_owner=rule_owner)]
        self._check_call_list(
            expected_calls,
            mgr.manage_contract_subject_in_filter.call_args_list)

        expected_calls = [
            mock.call(ctr['id'], ctr['id'], rules[out]['id'],
                      owner=ctr['tenant_id'], transaction='transaction',
                      unset=False, rule_owner=rule_owner),
            mock.call(ctr['id'], ctr['id'],
                      amap.REVERSE_PREFIX + rules[in_d]['id'],
                      owner=ctr['tenant_id'], transaction='transaction',
                      unset=False, rule_owner=rule_owner)]
        self._check_call_list(
            expected_calls,
            mgr.manage_contract_subject_out_filter.call_args_list)

        # Create policy_rule_set with BI rule
        ctr = self.create_policy_rule_set(
            name="ctr", policy_rules=[rules[bi]['id']])['policy_rule_set']

        mgr.manage_contract_subject_in_filter.call_happened_with(
            ctr['id'], ctr['id'], rules[bi]['id'], owner=ctr['tenant_id'],
            transaction='transaction', unset=False,
            rule_owner=rule_owner)
        mgr.manage_contract_subject_out_filter.call_happened_with(
            ctr['id'], ctr['id'], rules[bi]['id'], owner=ctr['tenant_id'],
            transaction='transaction', unset=False,
            rule_owner=rule_owner)
        mgr.manage_contract_subject_in_filter.call_happened_with(
            ctr['id'], ctr['id'], amap.REVERSE_PREFIX + rules[bi]['id'],
            owner=ctr['tenant_id'], transaction='transaction', unset=False,
            rule_owner=rule_owner)
        mgr.manage_contract_subject_out_filter.call_happened_with(
            ctr['id'], ctr['id'], amap.REVERSE_PREFIX + rules[bi]['id'],
            owner=ctr['tenant_id'], transaction='transaction', unset=False,
            rule_owner=rule_owner)

    def test_policy_rule_set_created_with_rules(self):
        self._test_policy_rule_set_created_with_rules()

    def test_policy_rule_set_created_with_rules_shared(self):
        self._test_policy_rule_set_created_with_rules(shared=True)

    def _test_policy_rule_set_updated_with_new_rules(self, shared=False):
        bi, in_d, out = range(3)
        old_rules = self._create_3_direction_rules(shared=shared)
        new_rules = self._create_3_direction_rules(shared=shared)
        # exclude BI rule for now
        ctr = self.create_policy_rule_set(
            name="ctr",
            policy_rules=[x['id'] for x in old_rules[1:]])['policy_rule_set']
        data = {'policy_rule_set': {
            'policy_rules': [x['id'] for x in new_rules[1:]]}}
        rule_owner = (self.common_tenant if shared else
                      old_rules[in_d]['tenant_id'])
        mgr = self.driver.apic_manager
        mgr.manage_contract_subject_in_filter = MockCallRecorder()
        mgr.manage_contract_subject_out_filter = MockCallRecorder()
        self.new_update_request(
            'policy_rule_sets', data, ctr['id'], self.fmt).get_response(
                self.ext_api)
        # Verify old IN rule unset and new IN rule set
        self.assertTrue(
            mgr.manage_contract_subject_in_filter.call_happened_with(
                ctr['id'], ctr['id'], old_rules[in_d]['id'],
                rule_owner=rule_owner,
                owner=ctr['tenant_id'], transaction='transaction', unset=True))
        self.assertTrue(
            mgr.manage_contract_subject_in_filter.call_happened_with(
                ctr['id'], ctr['id'], new_rules[in_d]['id'],
                owner=ctr['tenant_id'], transaction='transaction',
                unset=False, rule_owner=rule_owner))
        self.assertTrue(
            mgr.manage_contract_subject_out_filter.call_happened_with(
                ctr['id'], ctr['id'], old_rules[out]['id'],
                owner=ctr['tenant_id'], transaction='transaction', unset=True,
                rule_owner=rule_owner))
        self.assertTrue(
            mgr.manage_contract_subject_out_filter.call_happened_with(
                ctr['id'], ctr['id'], new_rules[out]['id'],
                owner=ctr['tenant_id'], transaction='transaction',
                unset=False, rule_owner=rule_owner))

        ctr = self.create_policy_rule_set(
            name="ctr",
            policy_rules=[old_rules[0]['id']])['policy_rule_set']
        data = {'policy_rule_set': {'policy_rules': [new_rules[0]['id']]}}
        self.new_update_request(
            'policy_rule_sets', data, ctr['id'], self.fmt).get_response(
                self.ext_api)
        # Verify old BI rule unset and new Bu rule set
        self.assertTrue(
            mgr.manage_contract_subject_in_filter.call_happened_with(
                ctr['id'], ctr['id'], old_rules[bi]['id'],
                owner=ctr['tenant_id'], transaction='transaction', unset=True,
                rule_owner=rule_owner))
        self.assertTrue(
            mgr.manage_contract_subject_out_filter.call_happened_with(
                ctr['id'], ctr['id'], old_rules[bi]['id'],
                owner=ctr['tenant_id'], transaction='transaction', unset=True,
                rule_owner=rule_owner))
        self.assertTrue(
            mgr.manage_contract_subject_in_filter.call_happened_with(
                ctr['id'], ctr['id'], new_rules[bi]['id'],
                owner=ctr['tenant_id'], transaction='transaction',
                unset=False, rule_owner=rule_owner))
        self.assertTrue(
            mgr.manage_contract_subject_out_filter.call_happened_with(
                ctr['id'], ctr['id'], new_rules[bi]['id'],
                owner=ctr['tenant_id'], transaction='transaction',
                unset=False, rule_owner=rule_owner))

    def test_policy_rule_set_updated_with_new_rules(self):
        self._test_policy_rule_set_updated_with_new_rules()

    def test_policy_rule_set_updated_with_new_rules_shared(self):
        self._test_policy_rule_set_updated_with_new_rules(shared=True)

    def _create_3_direction_rules(self, shared=False):
        a1 = self.create_policy_action(name='a1',
                                       action_type='allow',
                                       shared=shared)['policy_action']
        cl_attr = {'protocol': 'tcp', 'port_range': 80}
        cls = []
        for direction in ['bi', 'in', 'out']:
            cls.append(self.create_policy_classifier(
                direction=direction, shared=shared,
                **cl_attr)['policy_classifier'])
        rules = []
        for classifier in cls:
            rules.append(self.create_policy_rule(
                policy_classifier_id=classifier['id'],
                policy_actions=[a1['id']],
                shared=shared)['policy_rule'])
        return rules


class TestPolicyRule(ApicMappingTestCase):

    def _test_policy_rule_created_on_apic(self, shared=False):
        pr = self._create_simple_policy_rule('in', 'udp', 88, shared=shared)

        tenant = self.common_tenant if shared else pr['tenant_id']
        mgr = self.driver.apic_manager
        expected_calls = [
            mock.call(pr['id'], owner=tenant, etherT='ip', prot='udp',
                      dToPort=88, dFromPort=88, transaction=mock.ANY),
            mock.call(amap.REVERSE_PREFIX + pr['id'], owner=tenant,
                      etherT='ip', prot='udp', sToPort=88, sFromPort=88,
                      transaction=mock.ANY)]
        self._check_call_list(
            expected_calls, mgr.create_tenant_filter.call_args_list)

    def test_policy_rule_created_on_apic(self):
        self._test_policy_rule_created_on_apic()

    def test_policy_rule_created_on_apic_shared(self):
        self._test_policy_rule_created_on_apic(shared=True)

    def test_policy_rule_many_actions_rejected(self):
        actions = [self.create_policy_action(
            action_type='allow')['policy_action']['id'] for x in range(2)]

        cls = self.create_policy_classifier(direction='in', protocol='udp',
                                            port_range=80)['policy_classifier']
        self.create_policy_rule(policy_classifier_id=cls['id'],
                                expected_res_status=400,
                                policy_actions=actions)

    def _test_policy_rule_deleted_on_apic(self, shared=False):
        pr = self._create_simple_policy_rule(shared=shared)
        req = self.new_delete_request('policy_rules', pr['id'], self.fmt)
        req.get_response(self.ext_api)

        tenant = self.common_tenant if shared else pr['tenant_id']
        mgr = self.driver.apic_manager
        expected_calls = [
            mock.call(pr['id'], owner=tenant, transaction=mock.ANY),
            mock.call(amap.REVERSE_PREFIX + pr['id'], owner=tenant,
                      transaction=mock.ANY)]
        self._check_call_list(
            expected_calls, mgr.delete_tenant_filter.call_args_list)

    def test_policy_rule_deleted_on_apic(self):
        self._test_policy_rule_deleted_on_apic()

    def test_policy_rule_deleted_on_apic_shared(self):
        self._test_policy_rule_deleted_on_apic(shared=True)


class TestExternalSegment(ApicMappingTestCase):

    def test_pat_rejected(self):
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        # Verify Rejected on create
        res = self.create_external_segment(
            name='supported', port_address_translation=True,
            expected_res_status=400)
        self.assertEqual('PATNotSupportedByApicDriver',
                         res['NeutronError']['type'])

        # Verify Rejected on Update
        es = self.create_external_segment(
            name='supported', expected_res_status=201,
            port_address_translation=False)['external_segment']
        res = self.update_external_segment(
            es['id'], expected_res_status=400, port_address_translation=True)
        self.assertEqual('PATNotSupportedByApicDriver',
                         res['NeutronError']['type'])

    def _test_create(self, shared=False):
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        self.create_external_segment(name='supported', expected_res_status=201,
                                     shared=shared)
        self.create_external_segment(name='unsupport', expected_res_status=201,
                                     shared=shared)

    def test_create(self):
        self._test_create(False)
        self._test_create(True)

    def test_update_unsupported_noop(self):
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es = self.create_external_segment(
            name='unsupport', cidr='192.168.0.0/24',
            external_routes=[{'destination': '0.0.0.0/0',
                              'nexthop': '192.168.0.254'},
                             {'destination': '128.0.0.0/16',
                              'nexthop': None}],
            expected_res_status=201)['external_segment']

        self.update_external_segment(es['id'], expected_res_status=200,
                                     external_routes=[])

        mgr = self.driver.apic_manager
        self.assertFalse(mgr.ensure_static_route_deleted.called)
        self.assertFalse(mgr.ensure_external_epg_routes_deleted.called)
        self.assertFalse(mgr.ensure_static_route_created.called)
        self.assertFalse(mgr.ensure_external_epg_created.called)
        self.assertFalse(mgr.ensure_next_hop_deleted.called)

    def _test_route_update_remove(self, shared_es, shared_ep):
        # Verify routes are updated correctly
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es = self.create_external_segment(
            name='supported', cidr='192.168.0.0/24', shared=shared_es,
            external_routes=[{'destination': '0.0.0.0/0',
                              'nexthop': '192.168.0.254'},
                             {'destination': '128.0.0.0/16',
                              'nexthop': None}],
            expected_res_status=201)['external_segment']

        # Attach 3 external policies
        f = self.create_external_policy
        eps = [f(external_segments=[es['id']], shared=shared_ep,
                 tenant_id=es['tenant_id'] if not shared_es else 'another',
                 expected_res_status=201)['external_policy']
               for x in xrange(3)]
        mgr = self.driver.apic_manager
        owner = es['tenant_id'] if not shared_es else self.common_tenant
        mgr.ensure_external_epg_created.reset_mock()
        # Remove route completely
        self.update_external_segment(es['id'], expected_res_status=200,
                                     external_routes=[
                                         {'destination': '0.0.0.0/0',
                                          'nexthop': '192.168.0.254'}])
        mgr = self.driver.apic_manager
        mgr.ensure_static_route_deleted.assert_called_with(
            es['id'], mocked.APIC_EXT_SWITCH, '128.0.0.0/16',
            owner=owner, transaction=mock.ANY)
        expected_delete_calls = []
        for ep in eps:
            expected_delete_calls.append(
                mock.call(es['id'], subnets=['128.0.0.0/16'],
                          external_epg=ep['id'], owner=owner,
                          transaction=mock.ANY))
        self._check_call_list(
            expected_delete_calls,
            mgr.ensure_external_epg_routes_deleted.call_args_list)
        self.assertFalse(mgr.ensure_static_route_created.called)
        self.assertFalse(mgr.ensure_external_epg_created.called)
        self.assertFalse(mgr.ensure_next_hop_deleted.called)

        # Remove nexthop only
        mgr.ensure_static_route_deleted.reset_mock()
        mgr.ensure_external_epg_routes_deleted.reset_mock()

        self.update_external_segment(es['id'], expected_res_status=200,
                                     external_routes=[
                                         {'destination': '0.0.0.0/0',
                                          'nexthop': None}])
        mgr.ensure_next_hop_deleted.assert_called_with(
            es['id'], mocked.APIC_EXT_SWITCH, '0.0.0.0/0', '192.168.0.254',
            owner=owner, transaction=mock.ANY)
        # Being the new nexthop 'None', the default one is used
        mgr.ensure_static_route_created.assert_called_with(
            es['id'], mocked.APIC_EXT_SWITCH, '192.168.0.1',
            subnet='0.0.0.0/0', owner=owner, transaction=mock.ANY)

        expected_delete_calls = []
        for ep in eps:
            expected_delete_calls.append(
                mock.call(es['id'], subnet='0.0.0.0/0', external_epg=ep['id'],
                          owner=owner, transaction=mock.ANY))
        self._check_call_list(expected_delete_calls,
                              mgr.ensure_external_epg_created.call_args_list)

        self.assertFalse(mgr.ensure_static_route_deleted.called)
        self.assertFalse(mgr.ensure_external_epg_routes_deleted.called)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_route_update_remove_1(self):
        self._test_route_update_remove(shared_ep=True, shared_es=True)

    def test_route_update_remove_2(self):
        self._test_route_update_remove(shared_ep=False, shared_es=True)

    def test_route_update_remove_3(self):
        self._test_route_update_remove(shared_ep=False, shared_es=False)

    def _test_route_update_add(self, shared_es, shared_ep):
        # Verify routes are updated correctly
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es = self.create_external_segment(
            name='supported', cidr='192.168.0.0/24', shared=shared_es,
            external_routes=[], expected_res_status=201)['external_segment']

        # Attach 3 external policies
        f = self.create_external_policy
        eps = [f(external_segments=[es['id']], shared=shared_ep,
                 tenant_id=es['tenant_id'] if not shared_es else 'another',
                 expected_res_status=201)['external_policy']
               for x in xrange(3)]
        mgr = self.driver.apic_manager
        mgr.ensure_external_epg_created.reset_mock()
        owner = es['tenant_id'] if not shared_es else self.common_tenant
        self.update_external_segment(es['id'], expected_res_status=200,
                                     external_routes=[
                                         {'destination': '128.0.0.0/16',
                                          'nexthop': '192.168.0.254'}])

        mgr.ensure_static_route_created.assert_called_with(
            es['id'], mocked.APIC_EXT_SWITCH, '192.168.0.254',
            subnet='128.0.0.0/16', owner=owner, transaction=mock.ANY)

        expected_create_calls = []
        for ep in eps:
            expected_create_calls.append(
                mock.call(es['id'], subnet='128.0.0.0/16',
                          external_epg=ep['id'], owner=owner,
                          transaction=mock.ANY))
        self._check_call_list(expected_create_calls,
                              mgr.ensure_external_epg_created.call_args_list)
        self.assertFalse(mgr.ensure_static_route_deleted.called)
        self.assertFalse(mgr.ensure_external_epg_routes_deleted.called)
        self.assertFalse(mgr.ensure_next_hop_deleted.called)

        mgr.ensure_static_route_created.reset_mock()
        mgr.ensure_external_epg_created.reset_mock()

        # Verify Route added with default gateway
        self.update_external_segment(es['id'], expected_res_status=200,
                                     external_routes=[
                                         {'destination': '128.0.0.0/16',
                                          'nexthop': '192.168.0.254'},
                                         {'destination': '0.0.0.0/0',
                                          'nexthop': None}])

        mgr.ensure_static_route_created.assert_called_with(
            es['id'], mocked.APIC_EXT_SWITCH, '192.168.0.1',
            subnet='0.0.0.0/0', owner=owner, transaction=mock.ANY)
        expected_create_calls = []
        for ep in eps:
            expected_create_calls.append(
                mock.call(es['id'], subnet='0.0.0.0/0',
                          external_epg=ep['id'], owner=owner,
                          transaction=mock.ANY))
        self._check_call_list(expected_create_calls,
                              mgr.ensure_external_epg_created.call_args_list)
        self.assertFalse(mgr.ensure_static_route_deleted.called)
        self.assertFalse(mgr.ensure_external_epg_routes_deleted.called)
        self.assertFalse(mgr.ensure_next_hop_deleted.called)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_route_update_add_1(self):
        self._test_route_update_add(shared_ep=True, shared_es=True)

    def test_route_update_add_2(self):
        self._test_route_update_add(shared_ep=False, shared_es=True)

    def test_route_update_add_3(self):
        self._test_route_update_add(shared_ep=False, shared_es=False)


class TestExternalPolicy(ApicMappingTestCase):

    def test_creation_noop(self):
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es = self.create_external_segment(
            name='supported', cidr='192.168.0.0/24',
            external_routes=[], expected_res_status=201)['external_segment']

        self.create_external_policy(
            external_segments=[es['id']], expected_res_status=201)
        # Verify not called since no routes are set
        mgr = self.driver.apic_manager
        self.assertFalse(
            mgr.ensure_external_epg_created.called,
            msg='calls:\n%s' %
                str(mgr.ensure_external_epg_created.call_args_list))

        es = self.create_external_segment(
            name='unsupported', cidr='192.168.0.0/24', expected_res_status=201,
            external_routes=[{'destination': '128.0.0.0/16',
                              'nexthop': '192.168.0.254'}])['external_segment']

        self.create_external_policy(
            external_segments=[es['id']], expected_res_status=201,)
        # Verify noop on unsupported
        self.assertFalse(mgr.ensure_external_epg_created.called)

    def _test_creation_no_prs(self, shared_es, shared_ep):
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es_list = [
            self.create_external_segment(
                name='supported', cidr='192.168.0.0/24', shared=shared_es,
                expected_res_status=201,
                external_routes=[{
                    'destination': '128.0.0.0/16',
                    'nexthop': '192.168.0.254'}])['external_segment']
            for x in range(3)]

        ep = self.create_external_policy(
            external_segments=[x['id'] for x in es_list], shared=shared_ep,
            tenant_id=es_list[0]['tenant_id'] if not shared_es else 'another',
            expected_res_status=201)['external_policy']

        mgr = self.driver.apic_manager
        owner = (es_list[0]['tenant_id'] if not shared_es
                 else self.common_tenant)
        expected_create_calls = []
        for es in es_list:
            expected_create_calls.append(
                mock.call(es['id'], subnet='128.0.0.0/16',
                external_epg=ep['id'], owner=owner,
                transaction=mock.ANY))
        self._check_call_list(expected_create_calls,
                              mgr.ensure_external_epg_created.call_args_list)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_creation_no_prs_1(self):
        self._test_creation_no_prs(shared_ep=True, shared_es=True)

    def test_creation_no_prs_2(self):
        self._test_creation_no_prs(shared_ep=False, shared_es=True)

    def test_creation_no_prs_3(self):
        self._test_creation_no_prs(shared_ep=False, shared_es=False)

    def _test_update_no_prs(self, shared_es, shared_ep):
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es_list = [
            self.create_external_segment(
                name='supported', cidr='192.168.0.0/24', shared=shared_es,
                expected_res_status=201,
                external_routes=[{
                    'destination': '128.0.0.0/16',
                    'nexthop': '192.168.0.254'}])['external_segment']
            for x in range(3)]
        ep = self.create_external_policy(
            tenant_id=es_list[0]['tenant_id'] if not shared_es else 'another',
            shared=shared_ep, expected_res_status=201)['external_policy']
        ep = self.update_external_policy(
            ep['id'], expected_res_status=200, tenant_id=ep['tenant_id'],
            external_segments=[x['id'] for x in es_list])['external_policy']
        mgr = self.driver.apic_manager
        owner = (es_list[0]['tenant_id'] if not shared_es
                 else self.common_tenant)
        expected_create_calls = []
        for es in es_list:
            expected_create_calls.append(
                mock.call(es['id'], subnet='128.0.0.0/16',
                external_epg=ep['id'], owner=owner, transaction=mock.ANY))
        self._check_call_list(expected_create_calls,
                              mgr.ensure_external_epg_created.call_args_list)

        ep = self.update_external_policy(
            ep['id'], expected_res_status=200, tenant_id=ep['tenant_id'],
            external_segments=[])['external_policy']
        mgr = self.driver.apic_manager
        expected_create_calls = []
        for es in es_list:
            expected_create_calls.append(
                mock.call(es['id'], owner=owner, external_epg=ep['id']))
        self._check_call_list(expected_create_calls,
                              mgr.ensure_external_epg_deleted.call_args_list)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_update_no_prs_1(self):
        self._test_update_no_prs(shared_ep=True, shared_es=True)

    def test_update_no_prs_2(self):
        self._test_update_no_prs(shared_ep=False, shared_es=True)

    def test_update_no_prs_3(self):
        self._test_update_no_prs(shared_ep=False, shared_es=False)

    def _test_create_with_prs(self, shared_es, shared_ep, shared_prs):
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es_list = [
            self.create_external_segment(
                name='supported', cidr='192.168.0.0/24', shared=shared_es,
                expected_res_status=201,
                external_routes=[{
                    'destination': '128.0.0.0/16',
                    'nexthop': '192.168.0.254'}])['external_segment']
            for x in range(3)]
        prov = self._create_policy_rule_set_on_shared(
            shared=shared_prs,
            tenant_id=es_list[0]['tenant_id'] if not (
                shared_es | shared_prs) else 'another')
        cons = self._create_policy_rule_set_on_shared(
            shared=shared_prs,
            tenant_id=es_list[0]['tenant_id'] if not (
                shared_es | shared_prs) else 'another')
        ep = self.create_external_policy(
            provided_policy_rule_sets={prov['id']: ''},
            consumed_policy_rule_sets={cons['id']: ''}, shared=shared_ep,
            tenant_id=es_list[0]['tenant_id'] if not shared_es else 'another',
            external_segments=[x['id'] for x in es_list],
            expected_res_status=201)['external_policy']
        mgr = self.driver.apic_manager
        owner = (es_list[0]['tenant_id'] if not shared_es
                 else self.common_tenant)
        expected_calls = []
        for es in es_list:
            expected_calls.append(
                mock.call(es['id'], prov['id'], external_epg=ep['id'],
                          provided=True, owner=owner,
                          transaction=mock.ANY))
            expected_calls.append(
                mock.call(es['id'], cons['id'], external_epg=ep['id'],
                          provided=False, owner=owner,
                          transaction=mock.ANY))
        self._check_call_list(expected_calls,
                              mgr.set_contract_for_external_epg.call_args_list)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_create_with_prs_1(self):
        self._test_create_with_prs(shared_es=True, shared_ep=True,
                                   shared_prs=True)

    def test_create_with_prs_2(self):
        self._test_create_with_prs(shared_es=True, shared_ep=False,
                                   shared_prs=True)

    def test_create_with_prs_3(self):
        self._test_create_with_prs(shared_es=True, shared_ep=False,
                                   shared_prs=False)

    def test_create_with_prs_4(self):
        self._test_create_with_prs(shared_es=False, shared_ep=False,
                                   shared_prs=False)

    def test_create_with_prs_5(self):
        self._test_create_with_prs(shared_es=False, shared_ep=False,
                                   shared_prs=True)

    def _test_update_add_prs(self, shared_es, shared_ep, shared_prs):
        self._mock_external_dict([('supported', '192.168.0.2/24')])
        es_list = [
            self.create_external_segment(
                name='supported', cidr='192.168.0.0/24', shared=shared_es,
                expected_res_status=201,
                external_routes=[{
                    'destination': '128.0.0.0/16',
                    'nexthop': '192.168.0.254'}])['external_segment']
            for x in range(3)]
        prov = self._create_policy_rule_set_on_shared(
            shared=shared_prs, tenant_id=es_list[0]['tenant_id'] if not (
                shared_es | shared_prs) else 'another')
        cons = self._create_policy_rule_set_on_shared(
            shared=shared_prs, tenant_id=es_list[0]['tenant_id'] if not (
                shared_es | shared_prs) else 'another')
        ep = self.create_external_policy(
            external_segments=[x['id'] for x in es_list], shared=shared_ep,
            tenant_id=es_list[0]['tenant_id'] if not shared_es else 'another',
            expected_res_status=201)['external_policy']
        ep = self.update_external_policy(
            ep['id'], expected_res_status=200, tenant_id=ep['tenant_id'],
            provided_policy_rule_sets={prov['id']: ''},
            consumed_policy_rule_sets={cons['id']: ''})['external_policy']
        mgr = self.driver.apic_manager
        owner = (es_list[0]['tenant_id'] if not shared_es
                 else self.common_tenant)
        expected_calls = []
        for es in es_list:
            expected_calls.append(
                mock.call(es['id'], prov['id'], external_epg=ep['id'],
                          provided=True, owner=owner, transaction=mock.ANY))
            expected_calls.append(
                mock.call(es['id'], cons['id'], external_epg=ep['id'],
                          provided=False, owner=owner, transaction=mock.ANY))
        self._check_call_list(expected_calls,
                              mgr.set_contract_for_external_epg.call_args_list)

        ep = self.update_external_policy(
            ep['id'], expected_res_status=200, provided_policy_rule_sets={},
            consumed_policy_rule_sets={},
            tenant_id=ep['tenant_id'])['external_policy']
        expected_calls = []
        for es in es_list:
            expected_calls.append(
                mock.call(es['id'], prov['id'], external_epg=ep['id'],
                          provided=True, owner=owner, transaction=mock.ANY))
            expected_calls.append(
                mock.call(es['id'], cons['id'], external_epg=ep['id'],
                          provided=False, owner=owner, transaction=mock.ANY))
        self._check_call_list(
            expected_calls, mgr.unset_contract_for_external_epg.call_args_list)

    # Although the naming convention used here has been chosen poorly,
    # I'm separating the tests in order to get the mock re-set.
    def test_update_add_prs_1(self):
        self._test_update_add_prs(shared_es=True, shared_ep=True,
                                  shared_prs=True)

    def test_update_add_prs_2(self):
        self._test_update_add_prs(shared_es=True, shared_ep=False,
                                  shared_prs=True)

    def test_update_add_prs_3(self):
        self._test_update_add_prs(shared_es=True, shared_ep=False,
                                  shared_prs=False)

    def test_update_add_prs_4(self):
        self._test_update_add_prs(shared_es=False, shared_ep=False,
                                  shared_prs=False)

    def test_update_add_prs_5(self):
        self._test_update_add_prs(shared_es=False, shared_ep=False,
                                  shared_prs=True)


class TestApicChains(ApicMappingTestCase):

    def _create_servicechain_spec(self, node_types=None):
        node_types = node_types or []
        if not node_types:
            node_types = ['LOADBALANCER']
        node_ids = []
        for node_type in node_types:
            node_ids.append(self._create_servicechain_node(node_type))
        data = {'servicechain_spec': {'tenant_id': self._tenant_id,
                                      'nodes': node_ids}}
        scs_req = self.new_create_request(
            SERVICECHAIN_SPECS, data, self.fmt)
        spec = self.deserialize(
            self.fmt, scs_req.get_response(self.ext_api))
        scs_id = spec['servicechain_spec']['id']
        return scs_id

    def _create_servicechain_node(self, node_type="LOADBALANCER"):
        data = {'servicechain_node': {'service_type': node_type,
                                      'tenant_id': self._tenant_id,
                                      'config': "{}"}}
        scn_req = self.new_create_request(SERVICECHAIN_NODES, data, self.fmt)
        node = self.deserialize(self.fmt, scn_req.get_response(self.ext_api))
        scn_id = node['servicechain_node']['id']
        return scn_id

    def _assert_proper_chain_instance(self, sc_instance, provider_ptg_id,
                                      policy_rule_set_id, scs_id_list):
        self.assertEqual(sc_instance['provider_ptg_id'], provider_ptg_id)
        self.assertEqual(scs_id_list, sc_instance['servicechain_specs'])

    def _create_tcp_redirect_rule(self, port_range, servicechain_spec_id):
        action = self.create_policy_action(
            action_type='redirect',
            action_value=servicechain_spec_id)['policy_action']
        classifier = self.create_policy_classifier(
            protocol='TCP', port_range=port_range,
            direction='bi')['policy_classifier']
        policy_rule = self.create_policy_rule(
            policy_classifier_id=classifier['id'],
            policy_actions=[action['id']])['policy_rule']
        return (action['id'], classifier['id'], policy_rule['id'])

    def _create_provider_consumer_ptgs(self, prs_id=None):
        policy_rule_set_dict = {prs_id: None} if prs_id else {}
        provider_ptg = self.create_policy_target_group(
            name="ptg1", provided_policy_rule_sets=policy_rule_set_dict)
        provider_ptg_id = provider_ptg['policy_target_group']['id']
        consumer_ptg = self.create_policy_target_group(
            name="ptg2",
            consumed_policy_rule_sets=policy_rule_set_dict)
        consumer_ptg_id = consumer_ptg['policy_target_group']['id']
        return (provider_ptg_id, consumer_ptg_id)

    def test_action_spec_value_update(self):
        scs_id = self._create_servicechain_spec()
        action_id, _, policy_rule_id = self._create_tcp_redirect_rule(
                                                        "20:90", scs_id)

        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        provider_ptg_id, consumer_ptg_id = self._create_provider_consumer_ptgs(
            policy_rule_set_id)
        sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance = sc_instances['servicechain_instances'][0]
        self._assert_proper_chain_instance(sc_instance, provider_ptg_id,
                                           policy_rule_set_id, [scs_id])

        data = {'servicechain_node': {'service_type': "FIREWALL",
                                      'tenant_id': self._tenant_id,
                                      'config': "{}"}}
        scn_req = self.new_create_request(SERVICECHAIN_NODES, data, self.fmt)
        new_node = self.deserialize(
                    self.fmt, scn_req.get_response(self.ext_api))
        new_scn_id = new_node['servicechain_node']['id']
        data = {'servicechain_spec': {'tenant_id': self._tenant_id,
                                      'nodes': [new_scn_id]}}
        scs_req = self.new_create_request(SERVICECHAIN_SPECS, data, self.fmt)
        new_spec = self.deserialize(
                    self.fmt, scs_req.get_response(self.ext_api))
        new_scs_id = new_spec['servicechain_spec']['id']
        action = {'policy_action': {'action_value': new_scs_id}}
        req = self.new_update_request('policy_actions', action, action_id)
        action = self.deserialize(self.fmt,
                                  req.get_response(self.ext_api))

        new_sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        self.assertEqual(len(new_sc_instances['servicechain_instances']), 1)
        new_sc_instance = new_sc_instances['servicechain_instances'][0]
        self.assertEqual(sc_instance['id'], new_sc_instance['id'])
        self.assertEqual([new_scs_id], new_sc_instance['servicechain_specs'])

        req = self.new_delete_request(
                'policy_target_groups', provider_ptg_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)

        sc_instances = self._list_service_chains()
        self.assertEqual(len(sc_instances['servicechain_instances']), 0)

    def test_screen_prss_with_redirect(self):
        pr1 = self._create_simple_policy_rule('in', 'udp', 88)
        pr2 = self._create_simple_policy_rule('in', 'udp', 89)
        action = self.create_policy_action(
            action_type='redirect')['policy_action']
        cls = self.create_policy_classifier(
            protocol='tcp')['policy_classifier']
        pr4 = self.create_policy_rule(
            policy_classifier_id=cls['id'],
            policy_actions=[action['id']])['policy_rule']
        prs1 = self.create_policy_rule_set(
            policy_rules=[pr1['id'], pr4['id']])['policy_rule_set']
        prs2 = self.create_policy_rule_set(
            policy_rules=[pr1['id'], pr2['id']])['policy_rule_set']
        prs3 = self.create_policy_rule_set(
            policy_rules=[pr1['id'], pr4['id']])['policy_rule_set']
        ctx = context.get_admin_context()
        ctx._plugin_context = ctx
        ctx._plugin = self.driver.gbp_plugin
        mapping = self.driver._screen_prss_with_redirect(ctx)
        # Verify all the contracts with redirect action found
        self.assertEqual({prs1['id'], prs3['id']},
                         {x['prs']['id'] for x in mapping})

        mapping = self.driver._screen_prss_with_redirect(
            ctx, to_screen=[prs1['id'], prs2['id']])
        # Verify only prs1 found
        self.assertEqual({prs1['id']}, {x['prs']['id'] for x in mapping})
        self.assertEqual(action['id'], mapping[0]['pa']['id'])
        self.assertEqual(pr4['id'], mapping[0]['pr']['id'])

    def test_chains_by_rule(self):
        scs_id = self._create_servicechain_spec()
        pr1 = self._create_simple_policy_rule('in', 'udp', 88)
        action = self.create_policy_action(
            action_type='redirect', action_value=scs_id)['policy_action']
        cls = self.create_policy_classifier(
            protocol='tcp')['policy_classifier']
        pr4 = self.create_policy_rule(
            policy_classifier_id=cls['id'],
            policy_actions=[action['id']])['policy_rule']
        prs1 = self.create_policy_rule_set(
            policy_rules=[pr1['id'], pr4['id']])['policy_rule_set']
        prs3 = self.create_policy_rule_set(
            policy_rules=[pr1['id'], pr4['id']])['policy_rule_set']
        ctx = context.get_admin_context()
        ctx._plugin_context = ctx
        ctx._plugin = self.driver.gbp_plugin

        ptg1 = self.create_policy_target_group(
            provided_policy_rule_sets={prs1['id']: ''})['policy_target_group']
        self.create_policy_target_group(
            consumed_policy_rule_sets={prs1['id']: ''})['policy_target_group']
        pr4['policy_rule_sets'] = [prs1['id'], prs3['id']]
        chains = self.driver._chains_by_rule(ctx, pr4)
        self.assertEqual(1, len(chains))
        self.assertEqual(ptg1['id'], chains[0].provider_ptg_id)
        self.assertEqual(prs1['id'], chains[0].policy_rule_set_id)

        chains = self.driver._chains_by_rule(ctx, pr4, [prs1['id']])
        self.assertEqual(1, len(chains))
        self.assertEqual(ptg1['id'], chains[0].provider_ptg_id)
        self.assertEqual(prs1['id'], chains[0].policy_rule_set_id)

        chains = self.driver._chains_by_rule(ctx, pr4, [prs3['id']])
        self.assertEqual(0, len(chains))

    def test_classifier_update_to_chain(self):
        scs_id = self._create_servicechain_spec()
        _, classifier_id, policy_rule_id = self._create_tcp_redirect_rule(
                                                            "20:90", scs_id)

        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        provider_ptg_id, consumer_ptg_id = self._create_provider_consumer_ptgs(
                                                            policy_rule_set_id)

        sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance = sc_instances['servicechain_instances'][0]
        self._assert_proper_chain_instance(sc_instance, provider_ptg_id,
                                           policy_rule_set_id, [scs_id])

        # Update classifier and verify instance is not recreated
        classifier = {'policy_classifier': {'port_range': "80"}}
        req = self.new_update_request('policy_classifiers',
                                      classifier, classifier_id)
        self.deserialize(self.fmt, req.get_response(self.ext_api))

        sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance_new = sc_instances['servicechain_instances'][0]
        self._assert_proper_chain_instance(sc_instance, provider_ptg_id,
                                           policy_rule_set_id, [scs_id])
        self.assertEqual(sc_instance, sc_instance_new)

        req = self.new_delete_request(
                'policy_target_groups', provider_ptg_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)

        sc_instances = self._list_service_chains()
        self.assertEqual(len(sc_instances['servicechain_instances']), 0)

    def test_redirect_multiple_ptgs_single_prs(self):
        scs_id = self._create_servicechain_spec()
        _, _, policy_rule_id = self._create_tcp_redirect_rule(
                                                "20:90", scs_id)

        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']

        #Create 2 provider and 2 consumer PTGs
        provider_ptg1 = self.create_policy_target_group(
            name="p_ptg1",
            provided_policy_rule_sets={policy_rule_set_id: None})
        provider_ptg1_id = provider_ptg1['policy_target_group']['id']
        self.create_policy_target_group(
            name="c_ptg1",
            consumed_policy_rule_sets={policy_rule_set_id: None})

        provider_ptg2 = self.create_policy_target_group(
            name="p_ptg2",
            provided_policy_rule_sets={policy_rule_set_id: None})
        provider_ptg2_id = provider_ptg2['policy_target_group']['id']
        self.create_policy_target_group(
            name="c_ptg2",
            consumed_policy_rule_sets={policy_rule_set_id: None})

        sc_instances = self._list_service_chains()
        # We should have 2 service chain instances (one per provider)
        self.assertEqual(len(sc_instances['servicechain_instances']), 2)
        sc_instances = sc_instances['servicechain_instances']
        sc_instances_provider_ptg_ids = set()
        sc_instances_consumer_ptg_ids = set()
        for sc_instance in sc_instances:
            sc_instances_provider_ptg_ids.add(sc_instance['provider_ptg_id'])
            sc_instances_consumer_ptg_ids.add(sc_instance['consumer_ptg_id'])
        expected_provider_ptg_ids = {provider_ptg1_id, provider_ptg2_id}
        self.assertEqual(expected_provider_ptg_ids,
                         sc_instances_provider_ptg_ids)

        # Deleting one provider should end up deleting the one service chain
        # Instance associated to it
        req = self.new_delete_request(
            'policy_target_groups', provider_ptg1_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)

        sc_instances = self._list_service_chains()
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance = sc_instances['servicechain_instances'][0]
        self.assertNotEqual(sc_instance['provider_ptg_id'], provider_ptg1_id)

        req = self.new_delete_request(
            'policy_target_groups', provider_ptg2_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)

        sc_instances = self._list_service_chains()
        # No more service chain instances when all the providers are deleted
        self.assertEqual(len(sc_instances['servicechain_instances']), 0)

    def test_redirect_to_chain(self):
        scs_id = self._create_servicechain_spec()
        _, _, policy_rule_id = self._create_tcp_redirect_rule(
                                                "20:90", scs_id)

        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        provider_ptg_id, consumer_ptg_id = self._create_provider_consumer_ptgs(
                                                            policy_rule_set_id)

        sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance = sc_instances['servicechain_instances'][0]
        self._assert_proper_chain_instance(sc_instance, provider_ptg_id,
                                           policy_rule_set_id, [scs_id])

        # Verify that PTG delete cleans up the chain instances
        req = self.new_delete_request(
            'policy_target_groups', provider_ptg_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)

        sc_instances = self._list_service_chains()
        self.assertEqual(len(sc_instances['servicechain_instances']), 0)

    def test_rule_update_updates_chain(self):
        scs_id = self._create_servicechain_spec()
        _, _, policy_rule_id = self._create_tcp_redirect_rule("20:90", scs_id)

        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        provider_ptg_id, consumer_ptg_id = self._create_provider_consumer_ptgs(
                                                            policy_rule_set_id)
        sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance = sc_instances['servicechain_instances'][0]
        self._assert_proper_chain_instance(sc_instance, provider_ptg_id,
                                           policy_rule_set_id, [scs_id])

        # Update policy rule with new classifier and verify instance is
        # recreated
        classifier = self.create_policy_classifier(
            protocol='TCP', port_range="80",
            direction='bi')['policy_classifier']

        policy_rule = {'policy_rule': {
                                'policy_classifier_id': classifier['id']}}
        req = self.new_update_request('policy_rules', policy_rule,
                                      policy_rule_id)

        sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance_new = sc_instances['servicechain_instances'][0]
        self._assert_proper_chain_instance(sc_instance, provider_ptg_id,
                                           policy_rule_set_id, [scs_id])
        self.assertEqual(sc_instance, sc_instance_new)

        scs_id2 = self._create_servicechain_spec()
        action = self.create_policy_action(
            action_type='redirect', action_value=scs_id2)['policy_action']
        self.update_policy_rule(policy_rule_id, policy_actions=[action['id']])

        # Verify SC instance changed
        sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance_new = sc_instances['servicechain_instances'][0]
        self._assert_proper_chain_instance(sc_instance_new, provider_ptg_id,
                                           policy_rule_set_id, [scs_id2])
        self.assertNotEqual(sc_instance, sc_instance_new)

        req = self.new_delete_request(
                'policy_target_groups', provider_ptg_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)

        sc_instances = self._list_service_chains()
        self.assertEqual(len(sc_instances['servicechain_instances']), 0)

    def test_update_ptg_with_redirect_prs(self):
        scs_id = self._create_servicechain_spec()
        _, _, policy_rule_id = self._create_tcp_redirect_rule(
                                                "20:90", scs_id)

        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        provider_ptg, consumer_ptg = self._create_provider_consumer_ptgs()

        sc_instances = self._list_service_chains()
        self.assertEqual(len(sc_instances['servicechain_instances']), 0)

        # We should have one service chain instance created when PTGs are
        # updated with provided and consumed prs
        self.update_policy_target_group(
                            provider_ptg,
                            provided_policy_rule_sets={policy_rule_set_id: ''},
                            consumed_policy_rule_sets={},
                            expected_res_status=200)
        self.update_policy_target_group(
                            consumer_ptg,
                            provided_policy_rule_sets={},
                            consumed_policy_rule_sets={policy_rule_set_id: ''},
                            expected_res_status=200)

        sc_instances = self._list_service_chains()
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance = sc_instances['servicechain_instances'][0]
        self._assert_proper_chain_instance(sc_instance, provider_ptg,
                                           policy_rule_set_id, [scs_id])

        # Verify that PTG update removing prs cleans up the chain instances
        self.update_policy_target_group(
                            provider_ptg,
                            provided_policy_rule_sets={},
                            consumed_policy_rule_sets={},
                            expected_res_status=200)

        sc_instances = self._list_service_chains()
        self.assertEqual(len(sc_instances['servicechain_instances']), 0)

    def test_chain_on_apic_create(self):
        scs_id = self._create_servicechain_spec(
            node_types=['FIREWALL_TRANSPARENT'])
        _, _, policy_rule_id = self._create_tcp_redirect_rule(
            "20:90", scs_id)
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])['policy_rule_set']
        # Create PTGs on same L2P
        l2p = self.create_l2_policy()['l2_policy']
        provider = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']
        consumer = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']

        mgr = self.driver.apic_manager
        mgr.reset_mock()
        # Provide the redirect contract
        self.update_policy_target_group(
            consumer['id'],
            consumed_policy_rule_sets={policy_rule_set['id']: ''})
        self.assertFalse(mgr.ensure_bd_created_on_apic.called)
        self.assertFalse(mgr.ensure_epg_created.called)

        # Now form the chain
        self.update_policy_target_group(
            provider['id'],
            provided_policy_rule_sets={policy_rule_set['id']: ''})

        sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance = sc_instances['servicechain_instances'][0]

        expected = [
            # Provider EPG provided PRS
            mock.call(provider['tenant_id'], provider['id'],
                      policy_rule_set['id'], provider=True,
                      contract_owner=policy_rule_set['tenant_id'],
                      transaction=mock.ANY),
            # Consumer EPG consumed PRS
            mock.call(consumer['tenant_id'], consumer['id'],
                      policy_rule_set['id'], provider=False,
                      contract_owner=policy_rule_set['tenant_id'],
                      transaction=mock.ANY)]

        self._verify_chain_set(provider, l2p, policy_rule_set,
                               sc_instance, 1, pre_set_contract_calls=expected)

        # New consumer doesn't trigger anything
        new_consumer = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']
        mgr.reset_mock()
        self.update_policy_target_group(
            new_consumer['id'],
            consumed_policy_rule_sets={policy_rule_set['id']: ''})

        self.assertFalse(mgr.ensure_bd_created_on_apic.called)
        self.assertFalse(mgr.ensure_epg_created.called)

    def test_chain_on_apic_delete(self):
        scs_id = self._create_servicechain_spec(
            node_types=['FIREWALL_TRANSPARENT', 'FIREWALL_TRANSPARENT'])
        _, _, policy_rule_id = self._create_tcp_redirect_rule(
            "20:90", scs_id)
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])['policy_rule_set']
        # Create PTGs on same L2P
        l2p = self.create_l2_policy()['l2_policy']
        provider = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']
        consumer = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']

        # Provide the redirect contract
        self.update_policy_target_group(
            provider['id'],
            provided_policy_rule_sets={policy_rule_set['id']: ''})

        # Now form the chain
        self.update_policy_target_group(
            consumer['id'],
            consumed_policy_rule_sets={policy_rule_set['id']: ''})

        sc_instances = self._list_service_chains()
        # We should have one service chain instance created now
        sc_instance = sc_instances['servicechain_instances'][0]

        # Dissolve the chain by disassociation
        mgr = self.driver.apic_manager
        mgr.reset_mock()
        self.update_policy_target_group(
            provider['id'], provided_policy_rule_sets={})
        # Provider EPG contract unset
        expected = mock.call(provider['tenant_id'], provider['id'],
                             policy_rule_set['id'], provider=True,
                             contract_owner=policy_rule_set['tenant_id'],
                             transaction=mock.ANY)
        self._verify_chain_unset(
            provider, l2p, policy_rule_set,
            sc_instance, 2, pre_unset_contract_calls=[expected])

    def test_service_ports_bound(self):
        scs_id = self._create_servicechain_spec(
            node_types=['LOADBALANCER', 'FIREWALL_TRANSPARENT', 'LOADBALANCER',
                        'FIREWALL_TRANSPARENT'])
        _, _, policy_rule_id = self._create_tcp_redirect_rule(
            "20:90", scs_id)
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])['policy_rule_set']
        # Create PTGs on same L2P
        l2p = self.create_l2_policy()['l2_policy']
        provider = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']
        consumer = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']

        # Provide the redirect contract
        self.update_policy_target_group(
            provider['id'],
            provided_policy_rule_sets={policy_rule_set['id']: ''})

        # Now form the chain
        self.update_policy_target_group(
            consumer['id'],
            consumed_policy_rule_sets={policy_rule_set['id']: ''})

        # Create LB pt
        pt_lb_1 = self.create_policy_target(
            policy_target_group_id=provider['id'],
            name='chain_provider_1_notransparent')['policy_target']
        pt_lb_2 = self.create_policy_target(
            policy_target_group_id=provider['id'],
            name='chain_provider_3_notransparent')['policy_target']
        # Create PTs for FW1
        pt_s_fw_1 = self.create_policy_target(
            policy_target_group_id=provider['id'],
            name='chain_provider_2_transparent')['policy_target']
        pt_d_fw_1 = self.create_policy_target(
            policy_target_group_id=provider['id'],
            name='chain_consumer_2_transparent')['policy_target']
        pt_s_fw_2 = self.create_policy_target(
            policy_target_group_id=provider['id'],
            name='chain_provider_4_transparent')['policy_target']
        pt_d_fw_2 = self.create_policy_target(
            policy_target_group_id=provider['id'],
            name='chain_consumer_4_transparent')['policy_target']
        pts = [pt_lb_1, pt_lb_2, pt_s_fw_1, pt_d_fw_1, pt_s_fw_2, pt_d_fw_2]
        # Bind all ports
        for pt in pts:
            self._bind_port_to_host(pt['port_id'], 'h1')

        # Verify that all the ports are chained correctly.
        sc_instances = self._list_service_chains()
        sc_instance = sc_instances['servicechain_instances'][0]
        sc_instance_id = sc_instance['id']

        # Verify first LB
        mapping = self.driver.get_gbp_details(
            context.get_admin_context(),
            device='tap%s' % pt_lb_1['port_id'], host='h1')
        self.assertEqual('0-' + sc_instance_id,
                         mapping['endpoint_group_name'])

        # Verify second LB
        mapping = self.driver.get_gbp_details(
            context.get_admin_context(),
            device='tap%s' % pt_lb_2['port_id'], host='h1')
        self.assertEqual('1-' + sc_instance_id, mapping['endpoint_group_name'])

        # Verify First FW
        mapping = self.driver.get_gbp_details(
            context.get_admin_context(),
            device='tap%s' % pt_s_fw_1['port_id'], host='h1')
        self.assertEqual('0-' + sc_instance_id,
                         mapping['endpoint_group_name'])
        mapping = self.driver.get_gbp_details(
            context.get_admin_context(),
            device='tap%s' % pt_d_fw_1['port_id'], host='h1')
        self.assertEqual('1-' + sc_instance_id,
                         mapping['endpoint_group_name'])

        # Verify Second FW
        mapping = self.driver.get_gbp_details(
            context.get_admin_context(),
            device='tap%s' % pt_s_fw_2['port_id'], host='h1')
        self.assertEqual('1-' + sc_instance_id,
                         mapping['endpoint_group_name'])
        mapping = self.driver.get_gbp_details(
            context.get_admin_context(),
            device='tap%s' % pt_d_fw_2['port_id'], host='h1')
        self.assertEqual('2-' + sc_instance_id,
                         mapping['endpoint_group_name'])

    def test_service_ports_bound_notransparent(self):
        scs_id = self._create_servicechain_spec(
            node_types=['LOADBALANCER', 'LOADBALANCER'])
        _, _, policy_rule_id = self._create_tcp_redirect_rule(
            "20:90", scs_id)
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=[policy_rule_id])['policy_rule_set']
        # Create PTGs on same L2P
        l2p = self.create_l2_policy()['l2_policy']
        provider = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']
        consumer = self.create_policy_target_group(
            l2_policy_id=l2p['id'])['policy_target_group']

        # Provide the redirect contract
        self.update_policy_target_group(
            provider['id'],
            provided_policy_rule_sets={policy_rule_set['id']: ''})

        # Now form the chain
        self.update_policy_target_group(
            consumer['id'],
            consumed_policy_rule_sets={policy_rule_set['id']: ''})

        # Create LB pt
        pt_lb_1 = self.create_policy_target(
            policy_target_group_id=provider['id'],
            name='chain_provider_1_notransparent')['policy_target']
        pt_lb_2 = self.create_policy_target(
            policy_target_group_id=provider['id'],
            name='chain_provider_2_notransparent')['policy_target']

        pts = [pt_lb_1, pt_lb_2]
        for pt in pts:
            self._bind_port_to_host(pt['port_id'], 'h1')

        # Verify first LB
        mapping = self.driver.get_gbp_details(
            context.get_admin_context(),
            device='tap%s' % pt_lb_1['port_id'], host='h1')
        self.assertEqual(provider['id'],
                         mapping['endpoint_group_name'])

        # Verify second LB
        mapping = self.driver.get_gbp_details(
            context.get_admin_context(),
            device='tap%s' % pt_lb_2['port_id'], host='h1')
        self.assertEqual(provider['id'], mapping['endpoint_group_name'])

    def _verify_chain_set(self, provider, l2p, policy_rule_set, sc_instance,
                          n_tnodes, pre_bd_create_calls=None,
                          pre_epg_create_calls=None,
                          pre_set_contract_calls=None):
        # One shadow BD created
        mgr = self.driver.apic_manager
        expected_calls = pre_bd_create_calls or []
        for x in xrange(n_tnodes):
            expected_calls.append(mock.call(
                l2p['tenant_id'], str(x) + '-' + sc_instance['id'],
                ctx_owner=l2p['tenant_id'], ctx_name=l2p['l3_policy_id'],
                allow_broadcast=True, transaction=mock.ANY))

        expected_calls = pre_epg_create_calls or []

        # Shadow EPG created on shadow BD
        for x in xrange(n_tnodes):
            expected_calls.append(
                mock.call(l2p['tenant_id'], '0-' + sc_instance['id'],
                          bd_owner=l2p['tenant_id'],
                          bd_name=str(x) + '-' + sc_instance['id'],
                          transaction=mock.ANY))
        if n_tnodes > 0:
            # Provider moved to 0th shadow BD
            expected_calls.append(
                mock.call(provider['tenant_id'], provider['id'],
                          bd_owner=l2p['tenant_id'],
                          bd_name='0-' + sc_instance['id']))
            # Shadow EPG created in original BD
            expected_calls.append(
                mock.call(l2p['tenant_id'],
                          str(n_tnodes) + '-' + sc_instance['id'],
                          bd_owner=l2p['tenant_id'], bd_name=l2p['id'],
                          transaction=mock.ANY))
        self._check_call_list(
            expected_calls, mgr.ensure_epg_created.call_args_list)

        expected_calls = pre_set_contract_calls or []

        if n_tnodes > 0:
            # cons-side Shadow EPG provides PRS
            expected_calls.append(
                mock.call(l2p['tenant_id'],
                          str(n_tnodes) + '-' + sc_instance['id'],
                          policy_rule_set['id'], provider=True,
                          contract_owner=policy_rule_set['tenant_id'],
                          transaction=mock.ANY))
            # 0th shadow consumes ANY
            expected_calls.append(
                mock.call(l2p['tenant_id'], '0-' + sc_instance['id'],
                          'any-' + sc_instance['id'], provider=False,
                          contract_owner=l2p['tenant_id']))
            # Provider provides ANY
            expected_calls.append(
                mock.call(l2p['tenant_id'], provider['id'],
                          'any-' + sc_instance['id'], provider=True,
                          contract_owner=l2p['tenant_id']))

        self._check_call_list(
            expected_calls, mgr.set_contract_for_epg.call_args_list)

    def _verify_chain_unset(self, provider, l2p, policy_rule_set, sc_instance,
                            n_tnodes, pre_bd_delete_calls=None,
                            pre_epg_deleted_calls=None,
                            pre_unset_contract_calls=None):
        # shadow BDs deleted
        mgr = self.driver.apic_manager
        expected_calls = pre_bd_delete_calls or []
        for x in xrange(n_tnodes):
            expected_calls.append(
                mock.call(l2p['tenant_id'], str(x) + '-' + sc_instance['id'],
                transaction=mock.ANY))
        self._check_call_list(
            expected_calls, mgr.delete_bd_on_apic.call_args_list)

        # Shadow EPGs deleted
        expected_calls = pre_epg_deleted_calls or []
        for x in xrange(n_tnodes):
            expected_calls.append(
                mock.call(l2p['tenant_id'], str(x) + '-' + sc_instance['id'],
                          transaction=mock.ANY))
        if n_tnodes > 0:
            expected_calls.append(
                mock.call(l2p['tenant_id'],
                          str(n_tnodes) + '-' + sc_instance['id'],
                          transaction=mock.ANY))

        self._check_call_list(
            expected_calls, mgr.delete_epg_for_network.call_args_list)

        # Provider moved to original BD
        if n_tnodes > 0:
            mgr.ensure_epg_created.assert_called_once_with(
                provider['tenant_id'], provider['id'],
                bd_owner=l2p['tenant_id'], bd_name=l2p['id'])

        # Contracts unset
        expected_calls = pre_unset_contract_calls or []
        if n_tnodes > 0:
            expected_calls.append(
                mock.call(l2p['tenant_id'], provider['id'],
                          'any-' + sc_instance['id'], provider=True,
                          contract_owner=l2p['tenant_id']))

        self._check_call_list(
            expected_calls, mgr.unset_contract_for_epg.call_args_list)

    def _list_service_chains(self):
        sc_instance_list_req = self.new_list_request(SERVICECHAIN_INSTANCES)
        res = sc_instance_list_req.get_response(self.ext_api)
        return self.deserialize(self.fmt, res)
