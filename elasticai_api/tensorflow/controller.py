# Copyright 2020 The ElasticDL Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time

import tensorflow as tf
from tensorflow.python.framework.errors_impl import UnknownError

from elasticai_api.common.base_controller import (
    DEFAULT_MAX_ALLREDUCE_RETRY_NUM,
    RETRY_ALLREDUCE_INTERVAL_SECS,
    AllReduceController,
)
from elasticai_api.util.log_utils import default_logger as logger

try:
    import horovod.tensorflow as hvd
    from horovod.tensorflow.functions import broadcast_variables

except ImportError:
    hvd = None


class TensorFlowV2AllReduceController(AllReduceController):
    """The controller is responsible for elastic training of
    TensorFlow eager execution using AllReduce.
    """

    def __init__(self, master_client, data_shard_service):
        super(TensorFlowV2AllReduceController, self).__init__(
            master_client, data_shard_service
        )
        self._model = None
        self._optimizer = None

    def set_broadcast_model(self, model):
        self._model = model

    def set_broadcast_optimizer(self, optimizer):
        self._optimizer = optimizer

    def broadcast(self):
        broadcast_variables(self._model.variables, root_rank=0)
        broadcast_variables(self._optimizer.variables(), root_rank=0)

    def train_one_batch_with_retries(self, func, *args, **kwargs):
        for _ in range(DEFAULT_MAX_ALLREDUCE_RETRY_NUM):
            try:
                self._broadcast_if_needed()
                result = func(*args, **kwargs)
                break
            except UnknownError as e:
                logger.warning(
                    "Failed to perform allreduce operation on "
                    "the gradients. Retrying..."
                )
                # Those error message show that the communication
                # to merge gradient fails and we can rebuild the
                # communication.
                if (
                    "HorovodAllreduce" in e.message
                    or "HorovodAllgather" in e.message
                    or "HorovodBroadcast" in e.message
                ):
                    time.sleep(3)
                    self._rendezvous_manager.init_horovod_if_needed()
        return result


class TensorFlowV1AllReduceController(AllReduceController):
    """The controller is responsible for elastic training of
    TensorFlow graph execution using AllReduce.
    """

    def __init__(self, master_client, master_addr):
        super(TensorFlowV1AllReduceController, self).__init__(
            master_client, master_addr
        )
        self._bcast_op = None

    def broadcast(self):
        if self._bcast_op is None:
            self._variables = tf.global_variables()
            self._bcast_op = broadcast_variables(self._variables, root_rank=0)
        session = tf.get_default_session()
        session.run(self._bcast_op)

    def train_one_batch_with_retries(self, func, *args, **kwargs):
        allreduce_success = False
        for _ in range(DEFAULT_MAX_ALLREDUCE_RETRY_NUM + 1):
            try:
                self._broadcast_if_needed()
                result = func(*args, **kwargs)
                allreduce_success = True
                break
            except UnknownError as e:
                logger.warning(
                    "Failed to perform allreduce operation on "
                    "the gradients. Retrying..."
                )
                # Those error message show that the communication
                # to merge gradient fails and we can rebuild the
                # communication.
                if (
                    "HorovodAllreduce" in e.message
                    or "HorovodAllgather" in e.message
                    or "HorovodBroadcast" in e.message
                ):
                    time.sleep(RETRY_ALLREDUCE_INTERVAL_SECS)
                    self._rendezvous_manager.init_horovod_if_needed()
        if not allreduce_success:
            raise RuntimeError("Failed to perform allreduce.")
        return result
