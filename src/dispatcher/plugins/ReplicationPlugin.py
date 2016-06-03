#
# Copyright 2015 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

import enum
import uuid
import io
import os
import errno
import time
import copy
import logging
from cache import CacheStore
from resources import Resource
from paramiko import RSAKey, SSHException
from datetime import datetime
from dateutil.parser import parse as parse_datetime
from task import Provider, Task, ProgressTask, VerifyException, TaskException, TaskWarning, query, TaskDescription
from freenas.dispatcher.rpc import RpcException, SchemaHelper as h, description, accepts, returns, private
from freenas.dispatcher.fd import FileDescriptor
from utils import get_replication_client, call_task_and_check_state
from freenas.utils import first_or_default
from freenas.utils.query import wrap

logger = logging.getLogger(__name__)


status_cache = CacheStore()
link_cache = CacheStore()


class ReplicationActionType(enum.Enum):
    SEND_STREAM = 1
    DELETE_SNAPSHOTS = 2
    CLEAR_SNAPSHOTS = 3
    DELETE_DATASET = 4


class ReplicationAction(object):
    def __init__(self, type, localfs, remotefs, **kwargs):
        self.type = type
        self.localfs = localfs
        self.remotefs = remotefs
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __getstate__(self):
        d = dict(self.__dict__)
        d['type'] = d['type'].name
        return d


@description('Provides information about replication tasks')
class ReplicationLinkProvider(Provider):
    @query('replication-link')
    def query(self, filter=None, params=None):
        def extend(obj):
            if status_cache.is_valid(obj['name']):
                obj['status'] = status_cache.get(obj['name'])
            return obj

        links = link_cache.query(*[], **{})

        links = list(map(extend, links))
        return wrap(links).query(*(filter or []), **(params or {}))

    def sync_query(self, filter=None, params=None):
        def extend(obj):
            if status_cache.is_valid(obj['name']):
                obj['status'] = status_cache.get(obj['name'])
            return obj

        links = self.datastore.query('replication.links')
        latest_links = []
        for link in links:
            latest_links.append(self.dispatcher.call_task_sync('replication.get_latest_link', link['name']))

        latest_links = list(map(extend, latest_links))
        return wrap(latest_links).query(*(filter or []), **(params or {}))

    def local_query(self, filter=None, params=None):
        return self.datastore.query(
            'replication.links', *(filter or []), **(params or {})
        )

    def get_one_local(self, name):
        if self.datastore.exists('replication.links', ('name', '=', name)):
            return self.datastore.get_one('replication.links', ('name', '=', name))
        else:
            return None

    @private
    def put_status(self, name, status):
        status_cache.put(name, status)
        self.dispatcher.dispatch_event('replication.link.changed', {
            'operation': 'update',
            'ids': [name]
        })

    @private
    def get_status(self, name):
        if status_cache.is_valid(name):
            return status_cache.get(name)
        else:
            return None

    @private
    def link_cache_put(self, link):
        link_cache.put(link['name'], link)

    @private
    def link_cache_remove(self, name):
        link_cache.remove(name)

    @private
    def datasets_from_link(self, link):
        datasets = []
        for dataset in link['datasets']:
            if link['recursive']:
                try:
                    dependencies = self.dispatcher.call_sync(
                        'zfs.dataset.query',
                        [('name', '~', '^{0}(/|$)'.format(dataset))],
                    )
                except RpcException:
                    raise RpcException(errno.ENOENT, 'Dataset {0} not found'.format(dataset))

                for dependency in dependencies:
                    if dependency not in datasets:
                        datasets.append(dependency)
            else:
                dependency = self.dispatcher.call_sync('zfs.dataset.query', [('name', '=', dataset)], {'single': True})
                if dependency:
                    if dependency not in datasets:
                        datasets.append(dependency)
                else:
                    raise RpcException(errno.ENOENT, 'Dataset {0} not found'.format(dataset))

        return datasets

    @private
    def get_reserved_shares(self, link_name):
        shares = []
        link = self.dispatcher.call_task_sync('replication.get_latest_link', link_name)
        datasets = self.dispatcher.call_sync('replication.link.datasets_from_link', link)
        for dataset in datasets:
            dataset_path = self.dispatcher.call_sync('volume.get_dataset_path', dataset['name'])
            shares.extend(
                wrap(
                    self.dispatcher.call_sync('share.get_dependencies', dataset_path, False, False)
                ).query(('immutable', '=', True))
            )

        return shares

    @private
    def get_reserved_containers(self, link_name):
        containers = []
        link = self.dispatcher.call_task_sync('replication.get_latest_link', link_name)
        datasets = self.dispatcher.call_sync('replication.link.datasets_from_link', link)
        for dataset in datasets:
            containers.extend(
                wrap(
                    self.dispatcher.call_sync('container.get_dependent', dataset['name'])
                ).query(('immutable', '=', True))
            )

        return containers

    @private
    def get_related_shares(self, link_name):
        shares = []
        link = self.dispatcher.call_task_sync('replication.get_latest_link', link_name)
        datasets = self.dispatcher.call_sync('replication.link.datasets_from_link', link)
        for dataset in datasets:
            dataset_path = self.dispatcher.call_sync('volume.get_dataset_path', dataset['name'])
            shares.extend(self.dispatcher.call_sync('share.get_dependencies', dataset_path, False, False))

        return shares

    @private
    def get_related_containers(self, link_name):
        containers = []
        link = self.dispatcher.call_task_sync('replication.get_latest_link', link_name)
        datasets = self.dispatcher.call_sync('replication.link.datasets_from_link', link)
        for dataset in datasets:
            container = self.dispatcher.call_sync('container.get_dependent', dataset['name'])
            if container:
                containers.extend(container)

        return containers


class ReplicationBaseTask(Task):
    def get_replication_state(self, link):
        is_master = False
        remote = ''
        ips = self.dispatcher.call_sync('network.config.get_my_ips')
        for ip in ips:
            for partner in link['partners']:
                if partner == ip and partner == link['master']:
                    is_master = True
        for partner in link['partners']:
            if partner not in ips:
                remote = partner

        return is_master, remote

    def set_datasets_readonly(self, datasets, readonly, client=None):
        for dataset in datasets:
            if client:
                call_task_and_check_state(
                    client,
                    'zfs.update',
                    dataset['name'],
                    {'readonly': {'value': 'on' if readonly else 'off'}}
                )
            else:
                self.join_subtasks(self.run_subtask(
                    'zfs.update',
                    dataset['name'],
                    {'readonly': {'value': 'on' if readonly else 'off'}}
                ))

    def set_datasets_mount(self, datasets, mount, recursive=False, client=None):
        for dataset in datasets:
            if client:
                call_task_and_check_state(
                    client,
                    'zfs.{0}mount'.format('' if mount else 'u'),
                    dataset['name'],
                    recursive
                )
            else:
                self.join_subtasks(self.run_subtask(
                    'zfs.{0}mount'.format('' if mount else 'u'),
                    dataset['name'],
                    recursive
                ))

    def set_datasets_mount_ro(self, link, readonly, client=None):
        all_datasets = self.dispatcher.call_sync('replication.link.datasets_from_link', link)
        parent_datasets = self.get_parent_datasets(link)
        if readonly:
            self.set_datasets_mount(parent_datasets, False, link['recursive'], client)
            self.set_datasets_readonly(all_datasets, True, client)
        else:
            self.set_datasets_readonly(all_datasets, False, client)
            self.set_datasets_mount(parent_datasets, True, link['recursive'], client)

    def remove_datastore_timestamps(self, link):
        out_link = {}
        for key in link:
            if '_at' not in key:
                out_link[key] = link[key]

        return out_link

    def check_datasets_valid(self, link):
        datasets = wrap(
            self.dispatcher.call_sync('replication.link.datasets_from_link', link)
        ).query(*[], **{'select': 'name'})
        is_master, remote = self.get_replication_state(link)

        links = self.dispatcher.call_sync('replication.link.sync_query', [('name', '!=', link['name'])])
        for dataset in datasets:
            for l in links:
                l_datasets = wrap(
                    self.dispatcher.call_sync('replication.link.datasets_from_link', l)
                ).query(*[], **{'select': 'name'})
                l_is_master, remote = self.get_replication_state(l)

                if dataset in l_datasets:
                    if l['bidirectional'] or link['bidirectional']:
                        raise TaskException(
                            errno.EACCES,
                            'Bi-directional replication cannot share dataset {0} with other replication: {1}.'.format(
                                dataset,
                                l['name'] if link['bidirectional'] else link['name']
                            )
                        )

                    if is_master != l_is_master:
                        raise TaskException(
                            errno.EACCES,
                            'Usage of dataset {0} conflicts with {1} replication. \
                            Datasets cannot be a source and a target of replication at the same time'.format(
                                dataset,
                                l['name']
                            )
                        )
        return True

    def get_parent_datasets(self, link):
        datasets_to_replicate = self.dispatcher.call_sync('replication.link.datasets_from_link', link)
        if not link['recursive']:
            return datasets_to_replicate

        datasets_names = wrap(datasets_to_replicate).query(*[], **{'select': 'name'})
        len_sorted_datasets = sorted(datasets_names, key=lambda item: (len(item), item))

        parent_datasets = []
        for parent_dataset in len_sorted_datasets:
            parent_datasets.append(parent_dataset)
            for dataset in datasets_names:
                if dataset.startswith(parent_dataset + '/'):
                    len_sorted_datasets.remove(dataset)

        result = []
        for dataset in parent_datasets:
            result.append(wrap(datasets_to_replicate).query(*[('name', '=', dataset)], **{'single': True}))

        return result


@description("Sets up a replication link")
@accepts(h.all_of(
        h.ref('replication-link'),
        h.required('name', 'partners', 'master', 'datasets', 'replicate_services', 'bidirectional', 'recursive')
    )
)
class ReplicationCreateTask(ReplicationBaseTask):
    @classmethod
    def early_describe(cls):
        return "Creating the replication link"

    def describe(self, link):
        return TaskDescription("Creating the replication link {name}", name=link.get('name', '') or '')

    def verify(self, link):
        partners = link['partners']
        name = link['name']
        ip_matches = False

        ips = self.dispatcher.call_sync('network.config.get_my_ips')
        for ip in ips:
            for partner in partners:
                if partner == ip:
                    ip_matches = True

        if not ip_matches:
            raise VerifyException(errno.EINVAL, 'Provided partner IPs do not create a valid pair. Check addresses.')

        if self.datastore.exists('replication.links', ('name', '=', name)):
            raise VerifyException(errno.EEXIST, 'Replication link with name {0} already exists'.format(name))

        if len(partners) != 2:
            raise VerifyException(
                errno.EINVAL,
                'Replication link can only have 2 partners. Value {0} is not permitted.'.format(len(partners))
            )

        if not len(link['datasets']):
            raise VerifyException(errno.ENOENT, 'At least one dataset have to be specified')

        for dataset in link['datasets']:
            if not self.dispatcher.call_sync('zfs.dataset.query', [('name', '=', dataset)], {'single': True}):
                raise VerifyException(errno.ENOENT, 'Dataset {0} does not exist'.format(dataset))

        if link['master'] not in partners:
            raise VerifyException(
                errno.EINVAL,
                'Replication master must be one of replication partners {0}, {1}'.format(*partners)
            )

        if link['replicate_services'] and not link['bidirectional']:
            raise VerifyException(
                errno.EINVAL,
                'Replication of services is available only when bi-directional replication is selected'
            )

        return []

    def run(self, link):
        link['id'] = link['name']
        if 'update_date' not in link:
            link['update_date'] = str(datetime.utcnow())

        self.check_datasets_valid(link)

        is_master, remote = self.get_replication_state(link)
        remote_client = get_replication_client(self.dispatcher, remote)

        remote_link = remote_client.call_sync('replication.link.get_one_local', link['name'])
        id = self.datastore.insert('replication.links', link)
        self.dispatcher.call_sync('replication.link.link_cache_put', link)
        if is_master:
            self.join_subtasks(self.run_subtask('replication.prepare_slave', link))
        if not remote_link:
            call_task_and_check_state(remote_client, 'replication.create', link)
        else:
            if self.remove_datastore_timestamps(remote_link) != self.remove_datastore_timestamps(link):
                raise TaskException(
                    errno.EEXIST,
                    'Replication link {0} already exists on {1}'.format(link['name'], remote)
                )

        self.dispatcher.dispatch_event('replication.link.changed', {
            'operation': 'create',
            'ids': [id]
        })

        self.dispatcher.register_resource(Resource('replication:{0}'.format(link['name'])), parents=['replication'])
        remote_client.disconnect()

    def rollback(self, link):
        if self.datastore.exists('replication.links', ('name', '=', link['name'])):
            self.datastore.delete('replication.links', link['name'])


@description("Ensures slave datasets have been created. Checks if services names are available on slave.")
@accepts(h.ref('replication-link'))
class ReplicationPrepareSlaveTask(ReplicationBaseTask):
    @classmethod
    def early_describe(cls):
        return "Preparing slave for replication link"

    def describe(self, link):
        return TaskDescription("Preparing slave for replication link {name}", name=link.get('name') or '')

    def verify(self, link):
        return []

    def run(self, link):
        def match_disk(empty_disks, path):
            disk_config = self.dispatcher.call_sync('disk.get_disk_config', path)
            matching_disks = sorted(empty_disks, key=lambda d: d['mediasize'])
            disk = first_or_default(lambda d: d['mediasize'] >= disk_config['mediasize'], matching_disks)
            if not disk:
                raise TaskException(
                    errno.ENOENT,
                    'Cannot create a disk match for local disk {0}.'
                    'There are no empty disks left on {1} with mediasize equal or greater than {2}.'.format(
                        disk_config['gdisk_name'],
                        remote,
                        disk_config['mediasize']
                    )
                )
            del empty_disks[empty_disks.index(disk)]
            return disk

        is_master, remote = self.get_replication_state(link)
        remote_client = get_replication_client(self.dispatcher, remote)

        if is_master:
            with self.dispatcher.get_lock('volumes'):
                datasets_to_replicate = self.dispatcher.call_sync('replication.link.datasets_from_link', link)
                root = self.dispatcher.call_sync('volume.get_volumes_root')

                for dataset in datasets_to_replicate:
                    if link['replicate_services']:
                        for share in self.dispatcher.call_sync('share.get_dependencies', os.path.join(root, dataset['name']), False, False):
                            remote_share = remote_client.call_sync(
                                'share.query',
                                [('name', '=', share['name'])],
                                {'single': True}
                            )
                            if remote_share:
                                raise TaskException(
                                    errno.EEXIST,
                                    'Share {0} already exists on {1}'.format(share['name'], remote)
                                )

                        container = self.dispatcher.call_sync('container.get_dependent', dataset['name'])[0]
                        if container:
                            remote_container = remote_client.call_sync(
                                'container.query',
                                [('name', '=', container['name'])],
                                {'single': True}
                            )
                            if remote_container:
                                raise TaskException(
                                    errno.EEXIST,
                                    'Container {0} already exists on {1}'.format(container['name'], remote)
                                )

                    sp_dataset = dataset['name'].split('/', 1)
                    volume_name = sp_dataset[0]
                    dataset_name = None
                    if len(sp_dataset) == 2:
                        dataset_name = sp_dataset[1]

                    vol = self.datastore.get_one('volumes', ('id', '=', volume_name))
                    if vol.get('encrypted'):
                        raise TaskException(
                            errno.EINVAL,
                            'Encrypted volumes cannot be included in replication links: {0}'.format(volume_name)
                        )

                    remote_volume = remote_client.call_sync(
                        'volume.query',
                        [('id', '=', volume_name)],
                        {'single': True}
                    )
                    if remote_volume:
                        if remote_volume.get('encrypted'):
                            raise TaskException(
                                errno.EINVAL,
                                'Encrypted volumes cannot be included in replication links: {0}'.format(volume_name)
                            )

                    remote_dataset = remote_client.call_sync(
                        'zfs.dataset.query',
                        [('name', '=', dataset['name'])],
                        {'single': True}
                    )

                    if not remote_dataset:
                        if not remote_volume:

                            empty_disks = remote_client.call_sync('disk.query', [('status.empty', '=', True)])
                            if len(empty_disks) == 0:
                                raise TaskException(
                                    errno.ENOENT,
                                    'There are no empty disks left on {0} to be choose from'.format(remote)
                                )
                            topology = vol['topology']

                            for group_type in topology:
                                for item in topology[group_type]:
                                    if item['type'] == 'disk':
                                        item['path'] = match_disk(empty_disks, item['path'])['path']
                                    else:
                                        for vdev in item['children']:
                                            vdev['path'] = match_disk(empty_disks, vdev['path'])['path']

                            try:
                                call_task_and_check_state(
                                    remote_client,
                                    'volume.create',
                                    {
                                        'id': vol['id'],
                                        'type': vol['type'],
                                        'params': {'encryption': False, 'mount': False},
                                        'topology': topology
                                    }
                                )
                            except RpcException as e:
                                raise TaskException(
                                    e.code,
                                    'Cannot create exact duplicate of {0} on {1}. Message: {2}'.format(
                                        volume_name,
                                        remote,
                                        e.message
                                    ),
                                    stacktrace=e.stacktrace
                                )

                        if dataset_name:
                            for idx, sub_dataset in enumerate(dataset_name.split('/')):
                                sub_dataset_name = volume_name + '/' + '/'.join(dataset_name.split('/')[0:idx+1])
                                remote_sub_dataset = remote_client.call_sync(
                                    'zfs.dataset.query',
                                    [('name', '=', sub_dataset_name)],
                                    {'single': True}
                                )
                                if not remote_sub_dataset:
                                    local_sub_dataset = self.dispatcher.call_sync(
                                        'zfs.dataset.query',
                                        [('name', '=', sub_dataset_name)],
                                        {'single': True}
                                    )
                                    try:
                                        dataset_properties = {
                                            'id': local_sub_dataset['name'],
                                            'volume': local_sub_dataset['pool']
                                        }
                                        if local_sub_dataset['mountpoint']:
                                            dataset_properties['mountpoint'] = local_sub_dataset['mountpoint']
                                        dataset_properties['mounted'] = False
                                        call_task_and_check_state(
                                            remote_client,
                                            'volume.dataset.create',
                                            dataset_properties
                                        )
                                    except RpcException as e:
                                        raise TaskException(
                                            e.code,
                                            'Cannot create exact duplicate of {0} on {1}. Message: {2}'.format(
                                                sub_dataset_name,
                                                remote,
                                                e.message
                                            ),
                                            stacktrace=e.stacktrace
                                        )

                self.set_datasets_mount_ro(link, True, remote_client)

        else:
            call_task_and_check_state(remote_client, 'replication.prepare_slave', link)

        remote_client.disconnect()


@description("Deletes replication link")
@accepts(str, bool)
class ReplicationDeleteTask(ReplicationBaseTask):
    @classmethod
    def early_describe(cls):
        return "Deleting the replication link"

    def describe(self, name, scrub=False):
        return TaskDescription("Deleting the replication link {name}", name=name)

    def verify(self, name, scrub=False):
        if not self.datastore.exists('replication.links', ('name', '=', name)):
            raise VerifyException(errno.ENOENT, 'Replication link {0} do not exist.'.format(name))

        return ['replication:{0}'.format(name)]

    def run(self, name, scrub=False):
        link = self.join_subtasks(self.run_subtask('replication.get_latest_link', name))[0]
        is_master, remote = self.get_replication_state(link)
        remote_client = None

        try:
            remote_client = get_replication_client(self.dispatcher, remote)
        except (SSHException, OSError) as e:
            self.add_warning(TaskWarning(
                e.code,
                'Remote is unreachable. Delete operation is being performed only locally.'
            ))

        if not is_master:
            for service in ['shares', 'containers']:
                for reserved_item in self.dispatcher.call_sync('replication.link.get_reserved_{0}'.format(service), name):
                    self.join_subtasks(self.run_subtask(
                        '{0}.export'.format(service),
                        reserved_item['id']
                    ))

            if scrub:
                with self.dispatcher.get_lock('volumes'):
                    datasets = reversed(self.dispatcher.call_sync('replication.link.datasets_from_link', link))
                    for dataset in datasets:
                        if len(dataset['name'].split('/')) == 1:
                            self.join_subtasks(self.run_subtask('volume.delete', dataset['name']))
                        else:
                            self.join_subtasks(self.run_subtask('volume.dataset.delete', dataset['name']))

        self.datastore.delete('replication.links', link['id'])
        self.dispatcher.call_sync('replication.link.link_cache_remove', link['name'])
        self.dispatcher.unregister_resource('replication:{0}'.format(link['name']))

        self.dispatcher.dispatch_event('replication.link.changed', {
            'operation': 'delete',
            'ids': [link['id']]
        })

        if remote_client:
            if remote_client.call_sync('replication.link.get_one_local', name):
                call_task_and_check_state(remote_client, 'replication.delete', name)
            remote_client.disconnect()


@description("Update a replication link")
@accepts(str, h.ref('replication-link'))
class ReplicationUpdateTask(ReplicationBaseTask):
    @classmethod
    def early_describe(cls):
        return "Updating the replication link"

    def describe(self, name, updated_fields):
        return TaskDescription("Updating the replication link {name}", name=name)

    def verify(self, name, updated_fields):
        if not self.datastore.exists('replication.links', ('name', '=', name)):
            raise VerifyException(errno.ENOENT, 'Replication link {0} do not exist.'.format(name))

        if 'datasets' in updated_fields:
            if not len(updated_fields['datasets']):
                raise VerifyException(errno.ENOENT, 'At least one dataset have to be specified')

            for dataset in updated_fields['datasets']:
                if not self.dispatcher.call_sync('zfs.dataset.query', [('name', '=', dataset)], {'single': True}):
                    raise VerifyException(errno.ENOENT, 'Dataset {0} does not exist'.format(dataset))

        return ['replication:{0}'.format(name)]

    def run(self, name, updated_fields):
        link = self.join_subtasks(self.run_subtask('replication.get_latest_link', name))[0]
        original_link = copy.deepcopy(link)
        old_name = link['name']
        is_master, remote = self.get_replication_state(link)
        remote_available = True
        remote_client = None
        try:
            remote_client = get_replication_client(self.dispatcher, remote)
        except TaskException as e:
            remote_available = False
            self.add_warning(TaskWarning(
                e.code,
                'Remote {0} is unreachable. Update is being performed only locally.'.format(remote)
            ))

        updated_fields['update_date'] = str(datetime.utcnow())

        if 'datasets' in updated_fields:
            if not remote_available:
                raise TaskException(
                    errno.EACCES,
                    'Cannot modify datasets, because remote {0} is unreachable'.format(remote)
                )

        link.update(updated_fields)

        partners = link['partners']
        if 'master' in updated_fields:
            if not updated_fields['master'] in partners:
                raise TaskException(
                    errno.EINVAL,
                    'Replication master must be one of replication partners {0}, {1}'.format(*partners)
                )

        if any(key in updated_fields for key in ['id', 'name', 'partners']):
            new_is_master, new_remote = self.get_replication_state(link)
            try:
                new_remote_client = get_replication_client(self.dispatcher, new_remote)
            except TaskException as e:
                raise TaskException(
                    e.code,
                    'New remote {0} is unreachable.'.format(new_remote)
                )
            if remote_available:
                self.join_subtasks(self.run_subtask('replication.delete', old_name))
                self.join_subtasks(self.run_subtask('replication.create', link))
            else:
                raise TaskException(errno.EACCES, 'Remote {0} is unreachable.'.format(remote))

            new_remote_client.disconnect()

        if any(key in updated_fields for key in ['recursive', 'datasets', 'bidirectional']):
            if not remote_available:
                raise TaskException(errno.EACCES, 'Remote {0} is unreachable.'.format(remote))
            self.check_datasets_valid(link)
            call_task_and_check_state(
                remote_client,
                'replication.check_datasets',
                self.remove_datastore_timestamps(link)
            )

        if link['replicate_services'] and not link['bidirectional']:
            raise TaskException(
                errno.EINVAL,
                'Replication of services is available only when bi-directional replication is selected'
            )

        if not link['replicate_services']:
            for service in ['shares', 'containers']:
                for reserved_item in self.dispatcher.call_sync('replication.link.get_reserved_{0}'.format(service), name):
                    self.join_subtasks(self.run_subtask(
                        '{0}.export'.format(service),
                        reserved_item['id']
                    ))

        if 'datasets' in updated_fields:
            self.set_datasets_mount_ro(
                original_link,
                False,
                remote_client if is_master else None
            )

            try:
                self.join_subtasks(self.run_subtask('replication.prepare_slave', link))
            except RpcException:
                self.set_datasets_mount_ro(
                    original_link,
                    True,
                    remote_client if is_master else None
                )
                raise

        if remote_available:
            try:
                call_task_and_check_state(
                    remote_client,
                    'replication.update_link',
                    self.remove_datastore_timestamps(link)
                )
            except TaskException as e:
                self.add_warning(TaskWarning(
                    e.code,
                    'Link update at remote side failed: {0}'.format(e.message)
                ))

        self.datastore.update('replication.links', link['id'], link)
        self.dispatcher.call_sync('replication.link.link_cache_put', link)

        if remote_available and link['replicate_services']:
            try:
                call_task_and_check_state(
                    remote_client,
                    'replication.reserve_services',
                    link['name']
                )
            except TaskException as e:
                self.add_warning(TaskWarning(
                    e.code,
                    'Service reservation failed: {0}'.format(e.message)
                ))

        self.dispatcher.dispatch_event('replication.link.changed', {
            'operation': 'update',
            'ids': [link['id']]
        })
        if remote_available:
            remote_client.emit_event('replication.link.changed', {
                'operation': 'update',
                'ids': [link['id']]
            })
            remote_client.disconnect()


@description("Runs replication process based on saved link")
@accepts(str, h.array(h.ref('replication-transport-plugin')))
class ReplicationSyncTask(ReplicationBaseTask):
    @classmethod
    def early_describe(cls):
        return "Synchronizing replication link"

    def describe(self, name, transport_plugins):
        return TaskDescription("Synchronizing replication link {name}", name=name)

    def verify(self, name, transport_plugins):
        if not self.datastore.exists('replication.links', ('name', '=', name)):
            raise VerifyException(errno.ENOENT, 'Replication link {0} do not exist.'.format(name))

        return ['replication:{0}'.format(name)]

    def run(self, name, transport_plugins):
        start_time = time.time()
        total_size = 0
        status = 'SUCCESS'
        message = ''
        speed = 0
        remote_client = None
        try:
            link = self.join_subtasks(self.run_subtask('replication.get_latest_link', name))[0]
            is_master, remote = self.get_replication_state(link)
            remote_client = get_replication_client(self.dispatcher, remote)
            if is_master:
                with self.dispatcher.get_lock('volumes'):
                    parent_datasets = self.get_parent_datasets(link)

                    for dataset in parent_datasets:
                        result = self.join_subtasks(self.run_subtask(
                            'replication.replicate_dataset',
                            dataset['name'],
                            {
                                'remote': remote,
                                'remote_dataset': dataset['name'],
                                'recursive': link['recursive'],
                                'nomount': True
                            },
                            transport_plugins
                        ))

                        total_size += result[0][1]

                    if link['replicate_services']:
                        call_task_and_check_state(remote_client, 'replication.reserve_services', name)
            else:
                call_task_and_check_state(
                    remote_client,
                    'replication.sync',
                    link['name'],
                    transport_plugins
                )

        except TaskException as e:
            status = 'FAILED'
            message = e.message
            raise
        finally:
            end_time = time.time()
            if start_time != end_time:
                speed = int(float(total_size) / float(end_time - start_time))

            status_dict = {
                'status': status,
                'message': message,
                'size': total_size,
                'speed': speed
            }
            self.dispatcher.call_sync('replication.link.put_status', name, status_dict)
            if remote_client:
                remote_client.call_sync('replication.link.put_status', name, status_dict)
                remote_client.disconnect()


@description("Creates name reservation for services subject to replication")
@accepts(str)
class ReplicationReserveServicesTask(ReplicationBaseTask):
    @classmethod
    def early_describe(cls):
        return "Reserving services for replication link"

    def describe(self, name):
        return TaskDescription("Reserving services for replication link {name}", name=name)

    def verify(self, name):
        if not self.datastore.exists('replication.links', ('name', '=', name)):
            raise VerifyException(errno.ENOENT, 'Replication link {0} do not exist.'.format(name))

        return []

    def run(self, name):
        service_types = ['shares', 'containers']
        link = self.join_subtasks(self.run_subtask('replication.get_latest_link', name))[0]
        is_master, remote = self.get_replication_state(link)
        remote_client = get_replication_client(self.dispatcher, remote)

        if is_master:
            call_task_and_check_state(remote_client, 'replication.reserve_services', name)
        else:
            if link['replicate_services']:
                for type in service_types:
                    services = remote_client.call_sync('replication.link.get_related_{0}'.format(type), name)
                    for service in services:
                        service['immutable'] = True
                        service['enabled'] = False
                        id = service['id']
                        if self.datastore.exists(type, ('id', '=', id)):
                            old_service = self.datastore.get_by_id(type, id)
                            if old_service != service:
                                self.datastore.update(type, id, service)
                                self.dispatcher.dispatch_event('{0}.changed'.format(type[:-1]), {
                                    'operation': 'update',
                                    'ids': [id]
                                })
                        else:
                            self.datastore.insert(type, service)
                            self.dispatcher.dispatch_event('{0}.changed'.format(type[:-1]), {
                                'operation': 'create',
                                'ids': [id]
                            })

            else:
                raise TaskException(errno.EINVAL, 'Selected link is not allowed to replicate services')

        remote_client.disconnect()


@description('Creates a snapshot of selected dataset')
@accepts(str, str, bool, int, str, bool)
@returns(str)
class SnapshotDatasetTask(Task):
    @classmethod
    def early_describe(cls):
        return "Creating a snapshot of ZFS dataset"

    def describe(self, dataset, recursive, lifetime, prefix='auto', replicable=False):
        return TaskDescription("Creating a snapshot of {name} ZFS dataset", name=dataset)

    def verify(self, dataset, recursive, lifetime, prefix='auto', replicable=False):
        if not self.dispatcher.call_sync('zfs.dataset.query', [('name', '=', dataset)], {'single': True}):
            raise VerifyException(errno.ENOENT, 'Dataset {0} not found'.format(dataset))

        return ['zfs:{0}'.format(dataset)]

    def run(self, dataset, recursive, lifetime, prefix='auto', replicable=False):
        snapname = '{0}-{1:%Y%m%d.%H%M}'.format(prefix, datetime.utcnow())
        params = {
            'org.freenas:uuid': {'value': str(uuid.uuid4())},
            'org.freenas:replicate': {'value': 'yes' if replicable else 'no'},
            'org.freenas:lifetime': {'value': str(lifetime)},
        }

        calendar_task_name = self.environment.get('calendar_task_name')
        if calendar_task_name:
            params['org.freenas:calendar_task'] = calendar_task_name

        base_snapname = snapname

        # Pick another name in case snapshot already exists
        for i in range(1, 99):
            if self.dispatcher.call_sync(
                'zfs.snapshot.query',
                [('name', '=', '{0}@{1}'.format(dataset, snapname))],
                {'count': True}
            ):
                snapname = '{0}-{1}'.format(base_snapname, i)
                continue

            break

        self.join_subtasks(self.run_subtask(
            'zfs.create_snapshot',
            dataset,
            snapname,
            recursive,
            params
        ))


@description('Calculates replication delta between datasets')
class CalculateReplicationDeltaTask(Task):
    @classmethod
    def early_describe(cls):
        return "Calculating replication delta"

    def describe(self, localds, remoteds, snapshots, recursive=False, followdelete=False):
        return TaskDescription(
            "Calculating replication delta between the {name} and the {remoteds}",
            name=localds,
            remoteds=remoteds
        )

    def verify(self, localds, remoteds, snapshots, recursive=False, followdelete=False):
        return ['zfs:{0}'.format(localds)]

    def run(self, localds, remoteds, snapshots_list, recursive=False, followdelete=False):
        datasets = [localds]
        remote_datasets = list(filter(lambda s: '@' not in s['name'], snapshots_list))
        actions = []

        def matches(src, tgt):
            return src['snapshot_name'] == tgt['snapshot_name'] and src['created_at'] == tgt['created_at']

        def match_snapshots(local, remote):
            for i in local:
                match = first_or_default(lambda s: matches(i, s), remote)
                if match:
                    yield i, match

        def convert_snapshot(snap):
            return {
                'name': snap['name'],
                'snapshot_name': snap['snapshot_name'],
                'created_at': datetime.fromtimestamp(int(snap['properties.creation.rawvalue'])),
                'uuid': snap.get('properties.org\\.freenas:uuid')
            }

        def extend_with_snapshot_name(snap):
            snap['snapshot_name'] = snap['name'].split('@')[-1] if '@' in snap['name'] else None

        for i in snapshots_list:
            extend_with_snapshot_name(i)

        if recursive:
            datasets = self.dispatcher.call_sync(
                'zfs.dataset.query',
                [('name', '~', '^{0}(/|$)'.format(localds))],
                {'select': 'name'}
            )

        for ds in datasets:
            localfs = ds
            remotefs = localfs.replace(localds, remoteds, 1)

            local_snapshots = list(map(
                convert_snapshot,
                wrap(self.dispatcher.call_sync('zfs.dataset.get_snapshots', localfs))
            ))

            remote_snapshots = wrap(snapshots_list).query(
                ('name', '~', '^{0}@'.format(remotefs))
            )

            snapshots = local_snapshots[:]
            found = None

            if remote_snapshots:
                # Find out the last common snapshot.
                pairs = list(match_snapshots(local_snapshots, remote_snapshots))
                if pairs:
                    pairs.sort(key=lambda p: p[0]['created_at'], reverse=True)
                    found, _ = first_or_default(None, pairs)

                logger.info('found = {0}'.format(found))

                if found:
                    if followdelete:
                        delete = []
                        for snap in remote_snapshots:
                            rsnap = snap['snapshot_name']
                            if not first_or_default(lambda s: s['snapshot_name'] == rsnap, local_snapshots):
                                delete.append(rsnap)

                        if delete:
                            actions.append(ReplicationAction(
                                ReplicationActionType.DELETE_SNAPSHOTS,
                                localfs,
                                remotefs,
                                snapshots=delete
                            ))

                    index = local_snapshots.index(found)

                    for idx in range(index + 1, len(local_snapshots)):
                        actions.append(ReplicationAction(
                            ReplicationActionType.SEND_STREAM,
                            localfs,
                            remotefs,
                            incremental=True,
                            anchor=local_snapshots[idx - 1]['snapshot_name'],
                            snapshot=local_snapshots[idx]['snapshot_name']
                        ))
                else:
                    actions.append(ReplicationAction(
                        ReplicationActionType.CLEAR_SNAPSHOTS,
                        localfs,
                        remotefs
                    ))

                    for idx in range(0, len(snapshots)):
                        actions.append(ReplicationAction(
                            ReplicationActionType.SEND_STREAM,
                            localfs,
                            remotefs,
                            incremental=idx > 0,
                            anchor=snapshots[idx - 1]['snapshot_name'] if idx > 0 else None,
                            snapshot=snapshots[idx]['snapshot_name']
                        ))
            else:
                logger.info('New dataset {0} -> {1}'.format(localfs, remotefs))
                for idx in range(0, len(snapshots)):
                    actions.append(ReplicationAction(
                        ReplicationActionType.SEND_STREAM,
                        localfs,
                        remotefs,
                        incremental=idx > 0,
                        anchor=snapshots[idx - 1]['snapshot_name'] if idx > 0 else None,
                        snapshot=snapshots[idx]['snapshot_name']
                    ))

        for rds in remote_datasets:
            remotefs = rds
            localfs = remotefs.replace(remoteds, localds, 1)

            if localfs not in datasets:
                actions.append(ReplicationAction(
                    ReplicationActionType.DELETE_DATASET,
                    localfs,
                    remotefs
                ))

        total_send_size = 0

        for action in actions:
            if action.type == ReplicationActionType.SEND_STREAM:
                size = self.dispatcher.call_sync(
                    'zfs.dataset.estimate_send_size',
                    action.localfs,
                    action.snapshot,
                    getattr(action, 'anchor', None)
                )

                action.send_size = size
                total_send_size += size

        return actions, total_send_size


@description("Runs a dataset replication with the specified arguments")
class ReplicateDatasetTask(ProgressTask):
    def __init__(self, dispatcher, datastore):
        super(ReplicateDatasetTask, self).__init__(dispatcher, datastore)
        self.rd_fd = None
        self.wr_fd = None
        self.aborted = False

    @classmethod
    def early_describe(cls):
        return "Replicating dataset"

    def describe(self, localds, options, transport_plugins=None, dry_run=False):
        return TaskDescription("Replicating dataset {name}", name=localds)

    def verify(self, localds, options, transport_plugins=None, dry_run=False):
        return ['zfs:{0}'.format(localds)]

    def run(self, localds, options, transport_plugins=None, dry_run=False):
        remote = options['remote']
        remoteds = options['remote_dataset']
        followdelete = options.get('followdelete', False)
        recursive = options.get('recursive', False)
        lifetime = options.get('lifetime', 365 * 24 * 60 * 60)
        force = options.get('force', True)
        nomount = options.get('nomount', False)

        self.join_subtasks(self.run_subtask(
            'volume.snapshot_dataset',
            localds,
            True,
            lifetime,
            'repl',
            True
        ))

        remote_client = get_replication_client(self.dispatcher, remote)

        def is_replicated(snapshot):
            if snapshot.get('properties.org\\.freenas:replicate.value') != 'yes':
                # Snapshot is not subject to replication
                return False

            return True

        self.set_progress(0, 'Reading replication state from remote side...')

        remote_datasets = wrap(remote_client.call_sync(
            'zfs.dataset.query',
            [('id', '~', '^{0}(/|$)'.format(remoteds))]
        ))

        remote_snapshots = wrap(remote_client.call_sync(
            'zfs.snapshot.query',
            [('id', '~', '^{0}(/|$|@)'.format(remoteds))]
        ))

        remote_data = []

        for i in remote_datasets:
            if not is_replicated(i):
                continue

            remote_data.append({
                'name': i['name'],
                'created_at': datetime.fromtimestamp(int(i['properties.creation.rawvalue'])),
                'uuid': i.get('properties.org\\.freenas:uuid.value')
            })

        for i in remote_snapshots:
            if not is_replicated(i):
                continue

            remote_data.append({
                'name': i['name'],
                'created_at': datetime.fromtimestamp(int(i['properties.creation.rawvalue'])),
                'uuid': i.get('properties.org\\.freenas:uuid.value')
            })

        (actions, send_size), = self.join_subtasks(self.run_subtask(
            'replication.calculate_delta',
            localds,
            remoteds,
            remote_data,
            recursive,
            followdelete
        ))

        if dry_run:
            return actions, send_size

        # 2nd pass - actual send
        for idx, action in enumerate(actions):
            if self.aborted:
                break

            progress = float(idx) / len(actions) * 100

            if action['type'] in (ReplicationActionType.DELETE_SNAPSHOTS.name, ReplicationActionType.CLEAR_SNAPSHOTS.name):
                self.set_progress(progress, 'Removing snapshots on remote dataset {0}'.format(action['remotefs']))
                # Remove snapshots on remote side
                result = remote_client.call_task_sync(
                    'zfs.delete_multiple_snapshots',
                    action['remotefs'],
                    action.get('snapshots')
                )

                if result['state'] != 'FINISHED':
                    raise TaskException(errno.EFAULT, 'Failed to destroy snapshots on remote end: {0}'.format(
                        result['error']['message']
                    ))

            if action['type'] == ReplicationActionType.SEND_STREAM.name:
                self.set_progress(progress, 'Sending {0} stream of snapshot {1}@{2}'.format(
                    'incremental' if action['incremental'] else 'full',
                    action['localfs'],
                    action['snapshot']
                ))

                self.rd_fd, self.wr_fd = os.pipe()
                fromsnap = action['anchor'] if 'anchor' in action else None

                self.join_subtasks(
                    self.run_subtask(
                        'zfs.send',
                        action['localfs'],
                        fromsnap,
                        action['snapshot'],
                        FileDescriptor(self.wr_fd)
                    ),
                    self.run_subtask(
                        'replication.transport.send',
                        FileDescriptor(self.rd_fd),
                        {
                            'client_address': remote,
                            'transport_plugins': transport_plugins,
                            'receive_properties': {
                                'name': action['remotefs'],
                                'force': force,
                                'nomount': nomount
                            },
                            'estimated_size': send_size
                        }
                    )
                )

            if action['type'] == ReplicationActionType.DELETE_DATASET.name:
                self.set_progress(progress, 'Removing remote dataset {0}'.format(action['remotefs']))
                result = remote_client.call_task_sync(
                    'zfs.destroy',
                    action['remotefs'].split('/')[0],
                    action['remotefs']
                )

                if result['state'] != 'FINISHED':
                    raise TaskException(errno.EFAULT, 'Failed to destroy dataset {0} on remote end: {1}'.format(
                        action['remotefs'],
                        result['error']['message']
                    ))

        remote_client.disconnect()

        return actions, send_size

    def abort(self):
        self.aborted = True
        self.rd_fd.close()
        self.wr_fd.close()


@description("Returns latest replication link of given name")
@accepts(str)
@returns(h.ref('replication-link'))
class ReplicationGetLatestLinkTask(ReplicationBaseTask):
    @classmethod
    def early_describe(cls):
        return "Fetching latest replication link"

    def describe(self, name):
        return TaskDescription("Fetching latest replication link {name}", name=name)

    def verify(self, name):
        if not self.datastore.exists('replication.links', ('name', '=', name)):
            raise VerifyException(errno.ENOENT, 'Replication link {0} do not exist.'.format(name))

        return []

    def run(self, name):
        local_link = self.dispatcher.call_sync('replication.link.get_one_local', name)
        ips = self.dispatcher.call_sync('network.config.get_my_ips')
        remote = ''
        client = None
        remote_link = None
        latest_link = local_link

        for partner in local_link['partners']:
            if partner not in ips:
                remote = partner

        try:
            client = get_replication_client(self.dispatcher, remote)
            remote_link = client.call_sync('replication.link.get_one_local', name)
            status = client.call_sync('replication.link.get_status', name)
            if not self.dispatcher.call_sync('replication.link.get_status', name):
                if status:
                    self.dispatcher.call_sync('replication.link.put_status', name, status)
        except RpcException:
            pass

        if remote_link:
            local_update_date = parse_datetime(local_link['update_date'])
            remote_update_date = parse_datetime(remote_link['update_date'])

            if local_update_date > remote_update_date:
                call_task_and_check_state(client, 'replication.update_link', self.remove_datastore_timestamps(local_link))
                client.emit_event('replication.link.changed', {
                    'operation': 'update',
                    'ids': [local_link['id']]
                })
            elif local_update_date < remote_update_date:
                self.join_subtasks(self.run_subtask('replication.update_link', remote_link))
                latest_link = remote_link
                self.dispatcher.dispatch_event('replication.link.changed', {
                    'operation': 'update',
                    'ids': [remote_link['id']]
                })

        if client:
            client.disconnect()
        return latest_link


@description("Updates local replication link entry if provided remote entry is newer")
@accepts(h.ref('replication-link'))
class ReplicationUpdateLinkTask(Task):
    @classmethod
    def early_describe(cls):
        return "Updating replication link"

    def describe(self, remote_link):
        return TaskDescription("Updating replication link {name}", name=remote_link['name'])

    def verify(self, remote_link):
        if not self.datastore.exists('replication.links', ('name', '=', remote_link['name'])):
            raise VerifyException(errno.ENOENT, 'Replication link {0} do not exist.'.format(remote_link['name']))

        return []

    def run(self, remote_link):
        local_link = self.dispatcher.call_sync('replication.link.get_one_local', remote_link['name'])
        for partner in local_link['partners']:
            if partner not in remote_link.get('partners', []):
                raise TaskException(
                    errno.EINVAL,
                    'One of remote link partners {0} do not match local link partners {1}, {2}'.format(
                        partner,
                        *remote_link['partners']
                    )
                )

        if parse_datetime(local_link['update_date']) < parse_datetime(remote_link['update_date']):
            self.datastore.update('replication.links', remote_link['id'], remote_link)
            self.dispatcher.call_sync('replication.link.link_cache_put', remote_link)


@description("Performs synchronization of actual role (master/slave) with replication link state")
@accepts(str)
class ReplicationRoleUpdateTask(ReplicationBaseTask):
    @classmethod
    def early_describe(cls):
        return "Synchronizing replication link role with it's desired state"

    def describe(self, name):
        return TaskDescription("Synchronizing replication link {name} role with it's desired state", name=name)

    def verify(self, name):
        if not self.datastore.exists('replication.links', ('name', '=', name)):
            raise VerifyException(errno.ENOENT, 'Replication link {0} do not exist.'.format(name))

        return ['replication:{0}'.format(name)]

    def run(self, name):
        link = self.join_subtasks(self.run_subtask('replication.get_latest_link', name))[0]
        if not link['bidirectional']:
            return

        is_master, remote = self.get_replication_state(link)
        datasets = self.dispatcher.call_sync('replication.link.datasets_from_link', link)
        current_readonly = self.dispatcher.call_sync(
            'zfs.dataset.query',
            [('name', '=', datasets[0]['name'])],
            {'single': True, 'select': 'properties.readonly.value'}
        )
        current_readonly = True if current_readonly == 'on' else False

        if (is_master and current_readonly) or (not is_master and not current_readonly):
            self.set_datasets_mount_ro(link, not current_readonly)
            if current_readonly:
                relation_type = 'reserved'
                action_type = False
            else:
                relation_type = 'related'
                action_type = True

            for service in ['shares', 'containers']:
                for reserved_item in self.dispatcher.call_sync('replication.link.get_{0}_{1}'.format(relation_type, service), name):
                    self.join_subtasks(self.run_subtask(
                        '{0}.immutable.set'.format(service[:-1]),
                        reserved_item['id'],
                        action_type
                    ))


@description("Checks if provided replication link would not conflict with other links")
@accepts(h.ref('replication-link'))
class ReplicationCheckDatasetsTask(ReplicationBaseTask):
    @classmethod
    def early_describe(cls):
        return "Checking for conflicts with replication link"

    def describe(self, link):
        return TaskDescription("Checking for conflicts with replication link {name}", name=link['name'])

    def verify(self, link):
        if not self.datastore.exists('replication.links', ('name', '=', link['name'])):
            raise VerifyException(errno.ENOENT, 'Replication link {0} do not exist.'.format(link['name']))

        return ['replication:{0}'.format(link['name'])]

    def run(self, link):
        self.check_datasets_valid(link)


def _depends():
    return ['NetworkPlugin', 'ServiceManagePlugin']


def _init(dispatcher, plugin):
    plugin.register_schema_definition('replication', {
        'type': 'object',
        'properties': {
            'remote': {'type': 'string'},
            'remote_dataset': {'type': 'string'},
            'followdelete': {'type': 'boolean'},
            'lifetime': {'type': ['number', 'null']},
            'transport_plugins': {'$ref': 'replication-transport-plugin'},
            'recursive': {'type': 'boolean'},
        },
        'additionalProperties': False,
    })

    plugin.register_schema_definition('replication-link', {
        'type': 'object',
        'properties': {
            'id': {'type': 'string'},
            'name': {'type': 'string'},
            'partners': {
                'type': 'array',
                'items': {'type': 'string'}
            },
            'master': {'type': 'string'},
            'update_date': {'type': 'string'},
            'datasets': {
                'type': 'array',
                'items': {'type': 'string'}
            },
            'bidirectional': {'type': 'boolean'},
            'replicate_services': {'type': 'boolean'},
            'recursive': {'type': 'boolean'},
            'status': {'$ref': 'replication-status'}
        },
        'additionalProperties': False,
    })

    plugin.register_schema_definition('replication-status', {
        'type': 'object',
        'properties': {
            'status': {'type': 'string'},
            'message': {'type': 'string'},
            'size': {'type': 'number'},
            'speed': {'type': 'number'}
        },
        'additionalProperties': False,
    })

    plugin.register_schema_definition('snapshot-info', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'name': {'type': 'string'},
            'created_at': {'type': 'string'},
            'uuid': {'type': ['string', 'null']},
            'type': {
                'type': 'string',
                'enum': ['FILESYSTEM', 'VOLUME']
            }
        }
    })

    dispatcher.register_resource(Resource('replication'))
    plugin.register_provider('replication.link', ReplicationLinkProvider)
    plugin.register_task_handler('volume.snapshot_dataset', SnapshotDatasetTask)
    plugin.register_task_handler('replication.calculate_delta', CalculateReplicationDeltaTask)
    plugin.register_task_handler('replication.replicate_dataset', ReplicateDatasetTask)
    plugin.register_task_handler('replication.get_latest_link', ReplicationGetLatestLinkTask)
    plugin.register_task_handler('replication.update_link', ReplicationUpdateLinkTask)
    plugin.register_task_handler('replication.create', ReplicationCreateTask)
    plugin.register_task_handler('replication.prepare_slave', ReplicationPrepareSlaveTask)
    plugin.register_task_handler('replication.update', ReplicationUpdateTask)
    plugin.register_task_handler('replication.sync', ReplicationSyncTask)
    plugin.register_task_handler('replication.role_update', ReplicationRoleUpdateTask)
    plugin.register_task_handler('replication.reserve_services', ReplicationReserveServicesTask)
    plugin.register_task_handler('replication.delete', ReplicationDeleteTask)
    plugin.register_task_handler('replication.check_datasets', ReplicationCheckDatasetsTask)

    plugin.register_event_type('replication.link.changed')

    # Generate replication key pair on first run
    if not dispatcher.configstore.get('replication.key.private') or not dispatcher.configstore.get('replication.key.public'):
        key = RSAKey.generate(bits=2048)
        buffer = io.StringIO()
        key.write_private_key(buffer)
        dispatcher.configstore.set('replication.key.private', buffer.getvalue())
        dispatcher.configstore.set('replication.key.public', key.get_base64())

    def on_etcd_resume(args):
        if args.get('name') != 'etcd.generation':
            return

        dispatcher.call_sync('etcd.generation.generate_group', 'replication')

    def on_replication_change(args):
        for i in args['ids']:
            dispatcher.call_task_sync('replication.role_update', i)

    def update_link_cache(args):
        sshd_service = dispatcher.call_sync('service.query', [('name', '=', 'sshd')], {'single': True})
        dispatcher.test_or_wait_for_event(
            'service.changed',
            lambda ar:
                ar['id'] == sshd_service['id'] and
                dispatcher.call_sync(
                    'service.query',
                    [('name', '=', 'sshd')],
                    {'select': 'state', 'single': True}
                ) == 'RUNNING',
            lambda: sshd_service['state'] == 'RUNNING',
            timeout=30
        )

        # Query, if possible, performs sync of replication links cache at both ends of each link
        links = dispatcher.call_sync('replication.link.sync_query')
        # And retry failed ones over encrypted link
        for link in links:
            try:
                dispatcher.call_task_sync('replication.reserve_services', link['name'])
                status = link.get('status')
                if status:
                    if status['status'] == 'FAILED':
                        dispatcher.call_task_sync(
                            'replication.sync',
                            link['name'],
                            [{
                                'name': 'encrypt',
                                'type': 'AES128'
                            }]
                        )
            except TaskException:
                pass

    plugin.register_event_handler('plugin.service_resume', on_etcd_resume)
    plugin.register_event_handler('replication.link.changed', on_replication_change)
    plugin.register_event_handler('network.changed', update_link_cache)
    links = dispatcher.call_sync('replication.link.local_query')
    for link in links:
        dispatcher.register_resource(Resource('replication:{0}'.format(link['name'])), parents=['replication'])
        dispatcher.call_sync('replication.link.link_cache_put', link)
