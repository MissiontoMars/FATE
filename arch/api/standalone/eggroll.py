#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import os
import pickle as c_pickle
from arch.api import StoreType
from arch.api.utils import cloudpickle as f_pickle, cache_utils, file_utils
from arch.api.utils.core import string_to_bytes, bytes_to_string
from heapq import heapify, heappop, heapreplace
from typing import Iterable
import uuid
from concurrent.futures import ProcessPoolExecutor as Executor
import lmdb
from cachetools import cached
import numpy as np
from functools import partial
from operator import is_not
import hashlib
import fnmatch
import shutil
import time
import socket
import random


class Standalone:
    __instance = None

    def __init__(self, job_id=None):
        self.data_dir = os.path.join(file_utils.get_project_base_directory(), 'data')
        self.job_id = str(uuid.uuid1()) if job_id is None else "{}".format(job_id)
        self.meta_table = _DTable('__META__', '__META__', 'fragments', 10)
        self.pool = Executor()
        Standalone.__instance = self

        self.unique_id_template = '_EggRoll_%s_%s_%s_%.20f_%d'

        # todo: move to EggRollContext
        try:
            self.host_name = socket.gethostname()
            self.host_ip = socket.gethostbyname(self.host_name)
        except socket.gaierror as e:
            self.host_name = 'unknown'
            self.host_ip = 'unknown'

    def table(self, name, namespace, partition=1, create_if_missing=True, error_if_exist=False, persistent=True):
        __type = StoreType.LMDB.value if persistent else StoreType.IN_MEMORY.value
        _table_key = ".".join([__type, namespace, name])
        self.meta_table.put_if_absent(_table_key, partition)
        partition = self.meta_table.get(_table_key)
        return _DTable(__type, namespace, name, partition)


    def parallelize(self, data: Iterable, include_key=False, name=None, partition=1, namespace=None,
                    create_if_missing=True,
                    error_if_exist=False,
                    persistent=False, chunk_size=100000):
        _iter = data if include_key else enumerate(data)
        if name is None:
            name = str(uuid.uuid1())
        if namespace is None:
            namespace = self.job_id
        __table = self.table(name, namespace, partition, persistent=persistent)
        __table.put_all(_iter, chunk_size=chunk_size)
        return __table


    def cleanup(self, name, namespace, persistent):
        if not namespace or not name:
            raise ValueError("neither name nor namespace can be blank")

        _type = StoreType.LMDB.value if persistent else StoreType.IN_MEMORY.value
        _base_dir = os.sep.join([Standalone.get_instance().data_dir, _type])
        if not os.path.isdir(_base_dir):
            raise EnvironmentError("illegal datadir set for eggroll")

        _namespace_dir = os.sep.join([_base_dir, namespace])
        if not os.path.isdir(_namespace_dir):
            raise EnvironmentError("namespace does not exist")

        _tables_to_delete = fnmatch.filter(os.listdir(_namespace_dir), name)
        for table in _tables_to_delete:
            shutil.rmtree(os.sep.join([_namespace_dir, table]))

    def generateUniqueId(self):
        return self.unique_id_template % (self.job_id, self.host_name, self.host_ip, time.time(), random.randint(10000, 99999))

    @staticmethod
    def get_instance():
        if Standalone.__instance is None:
            raise EnvironmentError("eggroll should initialize before use")
        return Standalone.__instance


def serialize(_obj):
    return c_pickle.dumps(_obj)


def _evict(_, env):
    env.close()


@cached(cache=cache_utils.EvictLRUCache(maxsize=64, evict=_evict))
def _open_env(path, write=False):
    os.makedirs(path, exist_ok=True)
    return lmdb.open(path, create=True, max_dbs=1, max_readers=1024, lock=write, sync=True, map_size=10_737_418_240)


def _get_db_path(*args):
    return os.sep.join([Standalone.get_instance().data_dir, *args])


def _get_env(*args, write=False):
    _path = _get_db_path(*args)
    return _open_env(_path, write=write)


def _hash_key_to_partition(key, partitions):
    _key = hashlib.sha1(key).digest()
    if isinstance(_key, bytes):
        _key = int.from_bytes(_key, byteorder='little', signed=False)
    if partitions < 1:
        raise ValueError('partitions must be a positive number')
    b, j = -1, 0
    while j < partitions:
        b = int(j)
        _key = ((_key * 2862933555777941757) + 1) & 0xffffffffffffffff
        j = float(b + 1) * (float(1 << 31) / float((_key >> 33) + 1))
    return int(b)


class _TaskInfo:
    def __init__(self, task_id, function_id, function_bytes):
        self._task_id = task_id
        self._function_id = function_id
        self._function_bytes = function_bytes


class _Operand:
    def __init__(self, _type, namespace, name, partition):
        self._type = _type
        self._namespace = namespace
        self._name = name
        self._partition = partition

    def __str__(self):
        return _get_db_path(self._type, self._namespace, self._name, str(self._partition))

    def as_env(self, write=False):
        return _get_env(self._type, self._namespace, self._name, str(self._partition), write=write)


class _UnaryProcess:
    def __init__(self, task_info: _TaskInfo, operand: _Operand):
        self._info = task_info
        self._operand = operand


class _BinaryProcess:
    def __init__(self, task_info: _TaskInfo, left: _Operand, right: _Operand):
        self._info = task_info
        self._left = left
        self._right = right


def __get_function(info: _TaskInfo):
    return f_pickle.loads(info._function_bytes)


def _generator_from_cursor(cursor):
    deserialize = c_pickle.loads
    for k, v in cursor:
        yield deserialize(k), deserialize(v)


def do_map(p: _UnaryProcess):
    _mapper = __get_function(p._info)
    op = p._operand
    rtn = _Operand(StoreType.IN_MEMORY.value, p._info._task_id, p._info._function_id, op._partition)
    source_env = op.as_env()
    serialize = c_pickle.dumps
    deserialize = c_pickle.loads
    _table_key = ".".join([op._type, op._namespace, op._name])
    txn_map = {}
    partitions = Standalone.get_instance().meta_table.get(_table_key)
    for p in range(partitions):
        env = _get_env(rtn._type, rtn._namespace, rtn._name, str(p), write=True)
        txn = env.begin(write=True)
        txn_map[p] = txn
    with source_env.begin() as source_txn:
        cursor = source_txn.cursor()
        for k_bytes, v_bytes in cursor:
            k, v = deserialize(k_bytes), deserialize(v_bytes)
            k1, v1 = _mapper(k, v)
            k1_bytes, v1_bytes = serialize(k1), serialize(v1)
            p = _hash_key_to_partition(k1_bytes, partitions)
            dest_txn = txn_map[p]
            dest_txn.put(k1_bytes, v1_bytes)
        cursor.close()
    for p, txn in txn_map.items():
        txn.commit()
    return rtn


def do_map_partitions(p: _UnaryProcess):
    _mapper = __get_function(p._info)
    op = p._operand
    rtn = _Operand(StoreType.IN_MEMORY.value, p._info._task_id, p._info._function_id, op._partition)
    source_env = op.as_env()
    dst_env = rtn.as_env(write=True)
    serialize = c_pickle.dumps
    with source_env.begin() as source_txn:
        with dst_env.begin(write=True) as dst_txn:
            cursor = source_txn.cursor()
            v = _mapper(_generator_from_cursor(cursor))
            if cursor.last():
                k_bytes = cursor.key()
                dst_txn.put(k_bytes, serialize(v))
            cursor.close()
    return rtn


def do_map_values(p: _UnaryProcess):
    _mapper = __get_function(p._info)
    op = p._operand
    rtn = _Operand(StoreType.IN_MEMORY.value, p._info._task_id, p._info._function_id, op._partition)
    source_env = op.as_env()
    dst_env = rtn.as_env(write=True)
    serialize = c_pickle.dumps
    deserialize = c_pickle.loads
    with source_env.begin() as source_txn:
        with dst_env.begin(write=True) as dst_txn:
            cursor = source_txn.cursor()
            for k_bytes, v_bytes in cursor:
                v = deserialize(v_bytes)
                v1 = _mapper(v)
                dst_txn.put(k_bytes, serialize(v1))
            cursor.close()
    return rtn


def do_join(p: _BinaryProcess):
    _joiner = __get_function(p._info)
    left_op = p._left
    right_op = p._right
    rtn = _Operand(StoreType.IN_MEMORY.value, p._info._task_id, p._info._function_id, left_op._partition)
    right_env = right_op.as_env()
    left_env = left_op.as_env()
    dst_env = rtn.as_env(write=True)
    serialize = c_pickle.dumps
    deserialize = c_pickle.loads
    with left_env.begin() as left_txn:
        with right_env.begin() as right_txn:
            with dst_env.begin(write=True) as dest_txn:
                cursor = left_txn.cursor()
                for k_bytes, v1_bytes in cursor:
                    v2_bytes = right_txn.get(k_bytes)
                    if v2_bytes is None:
                        continue
                    v1 = deserialize(v1_bytes)
                    v2 = deserialize(v2_bytes)
                    v3 = _joiner(v1, v2)
                    dest_txn.put(k_bytes, serialize(v3))
    return rtn


def do_reduce(p: _UnaryProcess):
    _reducer = __get_function(p._info)
    op = p._operand
    source_env = op.as_env()
    deserialize = c_pickle.loads
    value = None
    with source_env.begin() as source_txn:
        cursor = source_txn.cursor()
        for k_bytes, v_bytes in cursor:
            v = deserialize(v_bytes)
            if value is None:
                value = v
            else:
                value = _reducer(value, v)
    return value


def do_glom(p: _UnaryProcess):
    op = p._operand
    rtn = _Operand(StoreType.IN_MEMORY.value, p._info._task_id, p._info._function_id, op._partition)
    source_env = op.as_env()
    dst_env = rtn.as_env(write=True)
    serialize = c_pickle.dumps
    deserialize = c_pickle.loads
    with source_env.begin() as source_txn:
        with dst_env.begin(write=True) as dest_txn:
            cursor = source_txn.cursor()
            v_list = []
            k_bytes = None
            for k, v in cursor:
                v_list.append((deserialize(k), deserialize(v)))
                k_bytes = k
            if k_bytes is not None:
                dest_txn.put(k_bytes, serialize(v_list))
    return rtn


def do_sample(p: _UnaryProcess):
    op = p._operand
    rtn = _Operand(StoreType.IN_MEMORY.value, p._info._task_id, p._info._function_id, op._partition)
    source_env = op.as_env()
    dst_env = rtn.as_env(write=True)
    deserialize = c_pickle.loads
    fraction, seed = deserialize(p._info._function_bytes)
    with source_env.begin() as source_txn:
        with dst_env.begin(write=True) as dst_txn:
            cursor = source_txn.cursor()
            cursor.first()
            random_state = np.random.RandomState(seed)
            for k, v in cursor:
                if random_state.rand() < fraction:
                    dst_txn.put(k, v)
    return rtn

def do_subtract_by_key(p: _BinaryProcess):
    left_op = p._left
    right_op = p._right
    rtn = _Operand(StoreType.IN_MEMORY.value, p._info._task_id, p._info._function_id, left_op._partition)
    right_env = right_op.as_env()
    left_env = left_op.as_env()
    dst_env = rtn.as_env(write=True)
    serialize = c_pickle.dumps
    deserialize = c_pickle.loads
    with left_env.begin() as left_txn:
        with right_env.begin() as right_txn:
            with dst_env.begin(write=True) as dst_txn:
                cursor = left_txn.cursor()
                for k_bytes, left_v_bytes in cursor:
                    right_v_bytes = right_txn.get(k_bytes)
                    if right_v_bytes is None:
                        dst_txn.put(k_bytes, left_v_bytes)
                cursor.close()
    return rtn

def do_filter(p: _UnaryProcess):
    _func = __get_function(p._info)
    op = p._operand
    rtn = _Operand(StoreType.IN_MEMORY.value, p._info._task_id, p._info._function_id, op._partition)
    source_env = op.as_env()
    dst_env = rtn.as_env(write=True)
    serialize = c_pickle.dumps
    deserialize = c_pickle.loads
    with source_env.begin() as source_txn:
        with dst_env.begin(write=True) as dst_txn:
            cursor = source_txn.cursor()
            for k_bytes, v_bytes in cursor:
                k = deserialize(k_bytes)
                if _func(k):
                    dst_txn.put(k_bytes, v_bytes)
            cursor.close()
    return rtn

def do_union(p: _BinaryProcess):
    _func = __get_function(p._info)
    left_op = p._left
    right_op = p._right
    rtn = _Operand(StoreType.IN_MEMORY.value, p._info._task_id, p._info._function_id, left_op._partition)
    right_env = right_op.as_env()
    left_env = left_op.as_env()
    dst_env = rtn.as_env(write=True)
    serialize = c_pickle.dumps
    deserialize = c_pickle.loads
    with left_env.begin() as left_txn:
        with right_env.begin() as right_txn:
            with dst_env.begin(write=True) as dst_txn:
                # process left op
                left_cursor = left_txn.cursor()
                for k_bytes, left_v_bytes in left_cursor:
                    right_v_bytes = right_txn.get(k_bytes)
                    if right_v_bytes is None:
                        dst_txn.put(k_bytes, left_v_bytes)
                    else:
                        left_v = deserialize(left_v_bytes)
                        right_v = deserialize(right_v_bytes)
                        final_v = _func(left_v, right_v)
                        dst_txn.put(k_bytes, serialize(final_v))
                left_cursor.close()
                # process right op
                right_cursor = right_txn.cursor()
                for k_bytes, right_v_bytes in right_cursor:
                    final_v_bytes = dst_txn.get(k_bytes)
                    if final_v_bytes is None:
                        dst_txn.put(k_bytes, right_v_bytes)
                right_cursor.close()
    return rtn

# todo: abstraction
class _DTable(object):

    def __init__(self, _type, namespace, name, partitions):
        self._type = _type
        self._namespace = namespace
        self._name = name
        self._partitions = partitions
        self.schema = {}

    def __str__(self):
        return "type: {}, namespace: {}, name: {}, partitions: {}".format(self._type, self._namespace, self._name,
                                                                          self._partitions)

    def _get_env_for_partition(self, p: int, write=False):
        return _get_env(self._type, self._namespace, self._name, str(p), write=write)

    def kv_to_bytes(self, **kwargs):
        use_serialize = kwargs.get("use_serialize", True)
        # can not use is None
        if "k" in kwargs and "v" in kwargs:
            k, v = kwargs["k"], kwargs["v"]
            return (c_pickle.dumps(k), c_pickle.dumps(v)) if use_serialize \
                else (string_to_bytes(k), string_to_bytes(v))
        elif "k" in kwargs:
            k = kwargs["k"]
            return c_pickle.dumps(k) if use_serialize else string_to_bytes(k)
        elif "v" in kwargs:
            v = kwargs["v"]
            return c_pickle.dumps(v) if use_serialize else string_to_bytes(v)

    def put(self, k, v, use_serialize=True):
        k_bytes, v_bytes = self.kv_to_bytes(k=k, v=v, use_serialize=use_serialize)
        p = _hash_key_to_partition(k_bytes, self._partitions)
        env = self._get_env_for_partition(p, write=True)
        with env.begin(write=True) as txn:
            return txn.put(k_bytes, v_bytes)
        return False

    def count(self):
        cnt = 0
        for p in range(self._partitions):
            env = self._get_env_for_partition(p)
            cnt += env.stat()['entries']
        return cnt

    def delete(self, k, use_serialize=True):
        k_bytes = self.kv_to_bytes(k=k, use_serialize=use_serialize)
        p = _hash_key_to_partition(k_bytes, self._partitions)
        env = self._get_env_for_partition(p, write=True)
        with env.begin(write=True) as txn:
            old_value_bytes = txn.get(k_bytes)
            if txn.delete(k_bytes):
                return None if old_value_bytes is None else (c_pickle.loads(old_value_bytes) if use_serialize else old_value_bytes)
            return None

    def put_if_absent(self, k, v, use_serialize=True):
        k_bytes = self.kv_to_bytes(k=k, use_serialize=use_serialize)
        p = _hash_key_to_partition(k_bytes, self._partitions)
        env = self._get_env_for_partition(p, write=True)
        with env.begin(write=True) as txn:
            old_value_bytes = txn.get(k_bytes)
            if old_value_bytes is None:
                v_bytes = self.kv_to_bytes(v=v, use_serialize=use_serialize)
                txn.put(k_bytes, v_bytes)
                return None
            return c_pickle.loads(old_value_bytes) if use_serialize else old_value_bytes

    def put_all(self, kv_list: Iterable, use_serialize=True, chunk_size=100000):
        txn_map = {}
        _succ = True
        for p in range(self._partitions):
            env = self._get_env_for_partition(p, write=True)
            txn = env.begin(write=True)
            txn_map[p] = env, txn
        for k, v in kv_list:
            try:
                k_bytes, v_bytes = self.kv_to_bytes(k=k, v=v, use_serialize=use_serialize)
                p = _hash_key_to_partition(k_bytes, self._partitions)
                _succ = _succ and txn_map[p][1].put(k_bytes, v_bytes)
            except Exception as e:
                _succ = False
                break
        for p, (env, txn) in txn_map.items():
            txn.commit() if _succ else txn.abort()

    def get(self, k, use_serialize=True):
        k_bytes = self.kv_to_bytes(k=k, use_serialize=use_serialize)
        p = _hash_key_to_partition(k_bytes, self._partitions)
        env = self._get_env_for_partition(p)
        with env.begin(write=True) as txn:
            old_value_bytes = txn.get(k_bytes)
            return None if old_value_bytes is None else (c_pickle.loads(old_value_bytes) if use_serialize else old_value_bytes)

    def destroy(self):
        for p in range(self._partitions):
            env = self._get_env_for_partition(p, write=True)
            db = env.open_db()
            with env.begin(write=True) as txn:
                txn.drop(db)
        _table_key = ".".join([self._type, self._namespace, self._name])
        Standalone.get_instance().meta_table.delete(_table_key)
        _path = _get_db_path(self._type, self._namespace, self._name)
        import shutil
        shutil.rmtree(_path)

    def collect(self, min_chunk_size=0, use_serialize=True):
        iterators = []
        for p in range(self._partitions):
            env = self._get_env_for_partition(p)
            txn = env.begin()
            iterators.append(txn.cursor())
        return self._merge(iterators, use_serialize)

    def save_as(self, name, namespace, partition=None, use_serialize=True):
        if partition is None:
            partition = self._partitions
        dup = Standalone.get_instance().table(name, namespace, partition, persistent=True)
        dup.put_all(self.collect(use_serialize=use_serialize), use_serialize=use_serialize)
        return dup

    def take(self, n, keysOnly=False, use_serialize=True):
        if n <= 0:
            n = 1
        it = self.collect(use_serialize=use_serialize)
        rtn = list()
        i = 0
        for item in it:
            if keysOnly:
                rtn.append(item[0])
            else:
                rtn.append(item)
            i += 1
            if i == n:
                break
        return rtn

    def first(self, keysOnly=False, use_serialize=True):
        resp = self.take(1, keysOnly=keysOnly, use_serialize=use_serialize)
        if resp:
            return resp[0]
        else:
            return None

    @staticmethod
    def _merge(cursors, use_serialize=True):
        ''' Merge sorted iterators. '''
        entries = []
        for _id, it in enumerate(cursors):
            if it.next():
                key, value = it.item()
                entries.append([key, value, _id, it])
            else:
                it.close()
        heapify(entries)
        while entries:
            key, value, _, it = entry = entries[0]
            if use_serialize:
                yield c_pickle.loads(key), c_pickle.loads(value)
            else:
                yield bytes_to_string(key), value
            if it.next():
                entry[0], entry[1] = it.item()
                heapreplace(entries, entry)
            else:
                _, _, _, it = heappop(entries)
                it.close()

    @staticmethod
    def _serialize_and_hash_func(func):
        pickled_function = f_pickle.dumps(func)
        func_id = str(uuid.uuid1())
        return func_id, pickled_function

    @staticmethod
    def _repartition(dtable, partition_num, repartition_policy=None):
        return dtable.save_as(str(uuid.uuid1()), Standalone.get_instance().job_id, partition_num)

    def _submit_to_pool(self, func, _do_func):
        func_id, pickled_function = self._serialize_and_hash_func(func)
        _task_info = _TaskInfo(Standalone.get_instance().job_id, func_id, pickled_function)
        results = []
        for p in range(self._partitions):
            _op = _Operand(self._type, self._namespace, self._name, p)
            _p = _UnaryProcess(_task_info, _op)
            results.append(Standalone.get_instance().pool.submit(_do_func, _p))
        return results

    def map(self, func):
        results = self._submit_to_pool(func, do_map)
        for r in results:
            result = r.result()
        return Standalone.get_instance().table(result._name, result._namespace, self._partitions, persistent=False)

    def mapValues(self, func):
        results = self._submit_to_pool(func, do_map_values)
        for r in results:
            result = r.result()
        return Standalone.get_instance().table(result._name, result._namespace, self._partitions, persistent=False)

    def mapPartitions(self, func):
        results = self._submit_to_pool(func, do_map_partitions)
        for r in results:
            result = r.result()
        return Standalone.get_instance().table(result._name, result._namespace, self._partitions, persistent=False)

    def reduce(self, func):
        rs = [r.result() for r in self._submit_to_pool(func, do_reduce)]
        rs = [r for r in filter(partial(is_not, None), rs)]
        if len(rs) <= 0:
            return None
        rtn = rs[0]
        for r in rs[1:]:
            rtn = func(rtn, r)
        return rtn

    def glom(self):
        results = self._submit_to_pool(None, do_glom)
        for r in results:
            result = r.result()
        return Standalone.get_instance().table(result._name, result._namespace, self._partitions, persistent=False)

    def join(self, other, func):
        _job_id = Standalone.get_instance().job_id
        if other._partitions != self._partitions:
            if other.count() > self.count():
                return self.save_as(str(uuid.uuid1()), _job_id, partition=other._partitions).join(other,
                                                                                                  func)
            else:
                return self.join(other.save_as(str(uuid.uuid1()), _job_id, partition=self._partitions),
                                 func)
        func_id, pickled_function = self._serialize_and_hash_func(func)
        _task_info = _TaskInfo(_job_id, func_id, pickled_function)
        results = []
        for p in range(self._partitions):
            _left = _Operand(self._type, self._namespace, self._name, p)
            _right = _Operand(other._type, other._namespace, other._name, p)
            _p = _BinaryProcess(_task_info, _left, _right)
            results.append(Standalone.get_instance().pool.submit(do_join, _p))
        for r in results:
            result = r.result()
        return Standalone.get_instance().table(result._name, result._namespace, self._partitions, persistent=False)

    def sample(self, fraction, seed=None):
        results = self._submit_to_pool((fraction, seed), do_sample)
        for r in results:
            result = r.result()
        return Standalone.get_instance().table(result._name, result._namespace, self._partitions, persistent=False)

    def subtractByKey(self, other):
        _job_id = Standalone.get_instance().job_id
        if other._partitions != self._partitions:
            if other.count() > self.count():
                return self.save_as(str(uuid.uuid1()), _job_id, partition=other._partitions).subtractByKey(other)
            else:
                return self.union(other.save_as(str(uuid.uuid1()), _job_id, partition=self._partitions))
        func_id, pickled_function = self._serialize_and_hash_func(self._namespace + '.' + self._name + '-' + other._namespace + '.' + other._name)
        _task_info = _TaskInfo(_job_id, func_id, pickled_function)
        results = []
        for p in range(self._partitions):
            _left = _Operand(self._type, self._namespace, self._name, p)
            _right = _Operand(other._type, other._namespace, other._name, p)
            _p = _BinaryProcess(_task_info, _left, _right)
            results.append(Standalone.get_instance().pool.submit(do_subtract_by_key, _p))
        for r in results:
            result = r.result()
        return Standalone.get_instance().table(result._name, result._namespace, self._partitions, persistent=False)

    def filter(self, func):
        results = self._submit_to_pool(func, do_filter)
        for r in results:
            result = r.result()
        return Standalone.get_instance().table(result._name, result._namespace, self._partitions, persistent=False)

    def union(self, other, func=lambda v1, v2 : v1):
        _job_id = Standalone.get_instance().job_id
        if other._partitions != self._partitions:
            if other.count() > self.count():
                return self.save_as(str(uuid.uuid1()), _job_id, partition=other._partitions).union(other,
                                                                                                  func)
            else:
                return self.union(other.save_as(str(uuid.uuid1()), _job_id, partition=self._partitions),
                                 func)
        func_id, pickled_function = self._serialize_and_hash_func(func)
        _task_info = _TaskInfo(_job_id, func_id, pickled_function)
        results = []
        for p in range(self._partitions):
            _left = _Operand(self._type, self._namespace, self._name, p)
            _right = _Operand(other._type, other._namespace, other._name, p)
            _p = _BinaryProcess(_task_info, _left, _right)
            results.append(Standalone.get_instance().pool.submit(do_union, _p))
        for r in results:
            result = r.result()
        return Standalone.get_instance().table(result._name, result._namespace, self._partitions, persistent=False)


