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

import sqlalchemy as sa
from sqlalchemy.ext import baked
from sqlalchemy import orm

from neutron.db import api as db_api
from neutron.db import models_v2
from neutron_lib import context as nctx
from oslo_db import exception as db_exc
from oslo_log import log as logging

from neutron_lib.db import model_base
from neutron_lib.plugins import directory

LOG = logging.getLogger(__name__)

BAKERY = baked.bakery(_size_alert=lambda c: LOG.warning(
    "sqlalchemy baked query cache size exceeded in %s" % __name__))


# REVISIT: Fix the misspelling of 'association'.
class HAIPAddressToPortAssocation(model_base.BASEV2):

    """Port Owner for HA IP Address.

    This table is used to store the mapping between the HA IP Address
    and the Port ID of the Neutron Port which currently owns this
    IP Address.
    """

    __tablename__ = 'apic_ml2_ha_ipaddress_to_port_owner'

    ha_ip_address = sa.Column(sa.String(64), nullable=False,
                              primary_key=True)
    port_id = sa.Column(sa.String(64), sa.ForeignKey('ports.id',
                                                     ondelete='CASCADE'),
                        nullable=False, primary_key=True)


class PortForHAIPAddress(object):

    def _get_ha_ipaddress(self, port_id, ipaddress, session=None):
        session = session or db_api.get_reader_session()

        query = BAKERY(lambda s: s.query(
            HAIPAddressToPortAssocation))
        query += lambda q: q.filter_by(
            port_id=sa.bindparam('port_id'),
            ha_ip_address=sa.bindparam('ipaddress'))
        return query(session).params(
            port_id=port_id, ipaddress=ipaddress).first()

    def get_port_for_ha_ipaddress(self, ipaddress, network_id,
                                  session=None):
        """Returns the Neutron Port ID for the HA IP Addresss."""
        session = session or db_api.get_reader_session()
        query = BAKERY(lambda s: s.query(
            HAIPAddressToPortAssocation))
        query += lambda q: q.join(
            models_v2.Port,
            models_v2.Port.id == HAIPAddressToPortAssocation.port_id)
        query += lambda q: q.filter(
            HAIPAddressToPortAssocation.ha_ip_address ==
            sa.bindparam('ipaddress'))
        query += lambda q: q.filter(
            models_v2.Port.network_id == sa.bindparam('network_id'))
        port_ha_ip = query(session).params(
            ipaddress=ipaddress, network_id=network_id).first()
        return port_ha_ip

    def get_ha_ipaddresses_for_port(self, port_id, session=None):
        """Returns the HA IP Addressses associated with a Port."""
        session = session or db_api.get_reader_session()

        query = BAKERY(lambda s: s.query(
            HAIPAddressToPortAssocation))
        query += lambda q: q.filter_by(
            port_id=sa.bindparam('port_id'))
        objs = query(session).params(
            port_id=port_id).all()

        return sorted([x['ha_ip_address'] for x in objs])

    def set_port_id_for_ha_ipaddress(self, port_id, ipaddress, session=None):
        """Stores a Neutron Port Id as owner of HA IP Addr (idempotent API)."""
        session = session or db_api.get_writer_session()
        try:
            with session.begin(subtransactions=True):
                obj = self._get_ha_ipaddress(port_id, ipaddress, session)
                if obj:
                    return obj
                else:
                    obj = HAIPAddressToPortAssocation(port_id=port_id,
                                                      ha_ip_address=ipaddress)
                    session.add(obj)
                    return obj
        except db_exc.DBDuplicateEntry:
            LOG.debug('Duplicate IP ownership entry for tuple %s',
                      (port_id, ipaddress))

    def delete_port_id_for_ha_ipaddress(self, port_id, ipaddress,
                                        session=None):
        session = session or db_api.get_writer_session()
        with session.begin(subtransactions=True):
            try:
                # REVISIT: Can this query be baked? The
                # sqlalchemy.ext.baked.Result class does not have a
                # delete() method, and adding delete() to the baked
                # query before executing it seems to result in the
                # params() not being evaluated.
                return session.query(
                    HAIPAddressToPortAssocation).filter_by(
                        port_id=port_id,
                        ha_ip_address=ipaddress).delete()
            except orm.exc.NoResultFound:
                return

    def get_ha_port_associations(self):
        session = db_api.get_reader_session()

        query = BAKERY(lambda s: s.query(
            HAIPAddressToPortAssocation))
        return query(session).all()


class HAIPOwnerDbMixin(object):

    def __init__(self):
        self.ha_ip_handler = PortForHAIPAddress()

    def _get_plugin(self):
        return directory.get_plugin()

    def update_ip_owner(self, ip_owner_info):
        ports_to_update = set()
        port_id = ip_owner_info.get('port')
        ipv4 = ip_owner_info.get('ip_address_v4')
        ipv6 = ip_owner_info.get('ip_address_v6')
        network_id = ip_owner_info.get('network_id')
        if not port_id or (not ipv4 and not ipv6):
            return ports_to_update
        LOG.debug("Got IP owner update: %s", ip_owner_info)
        core_plugin = self._get_plugin()
        # REVISIT: just use SQLAlchemy session and models_v2.Port?
        port = core_plugin.get_port(nctx.get_admin_context(), port_id)
        if not port:
            LOG.debug("Ignoring update for non-existent port: %s", port_id)
            return ports_to_update
        ports_to_update.add(port_id)
        for ipa in [ipv4, ipv6]:
            if not ipa:
                continue
            try:
                session = db_api.get_writer_session()
                with session.begin(subtransactions=True):
                    old_owner = self.ha_ip_handler.get_port_for_ha_ipaddress(
                        ipa, network_id or port['network_id'], session=session)
                    self.ha_ip_handler.set_port_id_for_ha_ipaddress(port_id,
                                                                    ipa,
                                                                    session)
                    if old_owner and old_owner['port_id'] != port_id:
                        self.ha_ip_handler.delete_port_id_for_ha_ipaddress(
                            old_owner['port_id'], ipa, session=session)
                        ports_to_update.add(old_owner['port_id'])
            except db_exc.DBReferenceError as dbe:
                LOG.debug("Ignoring FK error for port %s: %s", port_id, dbe)
        return ports_to_update
