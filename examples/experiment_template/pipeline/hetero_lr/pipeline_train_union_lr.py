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

import argparse

from pipeline.backend.pipeline import PipeLine
from pipeline.component import HeteroFeatureBinning, HeteroFeatureSelection, DataStatistics, Evaluation
from pipeline.component.union import Union
from pipeline.component.scale import FeatureScale
from pipeline.component.dataio import DataIO
from pipeline.component.hetero_lr import HeteroLR
from pipeline.component.intersection import Intersection
from pipeline.component.reader import Reader
from pipeline.interface.data import Data
from pipeline.interface.model import Model
from pipeline.utils.tools import load_job_config
from pipeline.runtime.entity import JobParameters


def main(config="../../config.yaml", namespace=""):
    # obtain config
    if isinstance(config, str):
        config = load_job_config(config)
    parties = config.parties
    guest = parties.guest[0]
    host = parties.host[0]
    arbiter = parties.arbiter[0]

    guest_train_data_0 = {"name":"breast_hetero_guest", "namespace": "experiment"}
    guest_train_data_1 = {"name":"breast_hetero_guest", "namespace": "experiment"}
    guest_test_data_0 = {"name": "breast_hetero_guest", "namespace": "experiment"}
    guest_test_data_1 = {"name": "breast_hetero_guest", "namespace": "experiment"}
    host_train_data_0 = {"name": "breast_hetero_host_tag_value", "namespace": "experiment"}
    host_train_data_1 = {"name": "breast_hetero_host_tag_value", "namespace": "experiment"}
    host_test_data_0 = {"name": "breast_hetero_host_tag_value", "namespace": "experiment"}
    host_test_data_1 = {"name": "breast_hetero_host_tag_value", "namespace": "experiment"}

    # initialize pipeline
    pipeline = PipeLine()
    # set job initiator
    pipeline.set_initiator(role='guest', party_id=guest)
    # set participants information
    pipeline.set_roles(guest=guest, host=host, arbiter=arbiter)

    # define Reader components to read in data
    reader_0 = Reader(name="reader_0")
    reader_1 = Reader(name="reader_1")
    reader_2 = Reader(name="reader_2")
    reader_3 = Reader(name="reader_3")
    # configure Reader for guest
    reader_0.get_party_instance(role='guest', party_id=guest).component_param(table=guest_train_data_0)
    reader_1.get_party_instance(role='guest', party_id=guest).component_param(table=guest_train_data_1)
    reader_2.get_party_instance(role='guest', party_id=guest).component_param(table=guest_test_data_0)
    reader_3.get_party_instance(role='guest', party_id=guest).component_param(table=guest_test_data_1)
    # configure Reader for host
    reader_0.get_party_instance(role='host', party_id=host).component_param(table=host_train_data_0)
    reader_1.get_party_instance(role='host', party_id=host).component_param(table=host_train_data_1)
    reader_2.get_party_instance(role='host', party_id=host).component_param(table=host_test_data_0)
    reader_3.get_party_instance(role='host', party_id=host).component_param(table=host_test_data_1)

    param = {
        "name": "union_0",
        "keep_duplicate": True
    }
    union_0 = Union(**param)
    param = {
        "name": "union_1",
        "keep_duplicate": True
    }
    union_1 = Union(**param)

    param = {
        "input_format": "tag",
        "with_label": False,
        "tag_with_value": True,
        "delimitor": ";",
        "output_format": "dense"
    }
    
    # define DataIO components
    dataio_0 = DataIO(name="dataio_0")  # start component numbering at 0
    dataio_1 = DataIO(name="dataio_1")  # start component numbering at 1

    # get DataIO party instance of guest
    dataio_0_guest_party_instance = dataio_0.get_party_instance(role='guest', party_id=guest)
    # configure DataIO for guest
    dataio_0_guest_party_instance.component_param(with_label=True, output_format="dense")
    # get and configure DataIO party instance of host
    dataio_0.get_party_instance(role='host', party_id=host).component_param(**param)
    dataio_1.get_party_instance(role='guest', party_id=guest).component_param(with_label=True)
    dataio_1.get_party_instance(role='host', party_id=host).component_param(**param)

    # define Intersection components
    intersection_0 = Intersection(name="intersection_0")
    intersection_1 = Intersection(name="intersection_1")

    param = {
        "name": 'hetero_feature_binning_0',
        "method": 'optimal',
        "optimal_binning_param": {
            "metric_method": "iv"
        },
        "bin_indexes": -1
    }
    hetero_feature_binning_0 = HeteroFeatureBinning(**param)
    statistic_0 = DataStatistics(name='statistic_0')
    param = {
        "name": 'hetero_feature_selection_0',
        "filter_methods": ["manually", "iv_filter", "statistic_filter"],
        "manually_param": {
            "filter_out_indexes": [1, 2],
            "filter_out_names": ["x2", "x3"]
        },
        "iv_param": {
            "metrics": ["iv", "iv"],
            "filter_type": ["top_k", "threshold"],
            "take_high": [True, True],
            "threshold": [10, 0.01]
        },
        "statistic_param": {
            "metrics": ["coefficient_of_variance", "skewness"],
            "filter_type": ["threshold", "threshold"],
            "take_high": [True, True],
            "threshold": [0.001, -0.01]
        },
        "select_col_indexes": -1
    }
    hetero_feature_selection_0 = HeteroFeatureSelection(**param)
    hetero_feature_selection_1 = HeteroFeatureSelection(name='hetero_feature_selection_1')
    param = {
        "name":"hetero_scale_0",
        "method":"standard_scale"        
    }
    hetero_scale_0 = FeatureScale(**param)
    hetero_scale_1 = FeatureScale(name='hetero_scale_1')
    param = {
        "penalty": "L2",
        "validation_freqs": None,
        "early_stopping_rounds": None,
        "max_iter": 5
    }


    hetero_lr_0 = HeteroLR(name='hetero_lr_0', **param)
    evaluation_0 = Evaluation(name='evaluation_0')
    # add components to pipeline, in order of task execution
    pipeline.add_component(reader_0)
    pipeline.add_component(reader_1)
    pipeline.add_component(reader_2)
    pipeline.add_component(reader_3)
    pipeline.add_component(union_0, data=Data(data=[reader_0.output.data, reader_1.output.data]))
    pipeline.add_component(union_1, data=Data(data=[reader_2.output.data, reader_3.output.data]))

    pipeline.add_component(dataio_0, data=Data(data=union_0.output.data))
    pipeline.add_component(dataio_1, data=Data(data=union_1.output.data), model=Model(dataio_0.output.model))
    # set data input sources of intersection components
    pipeline.add_component(intersection_0, data=Data(data=dataio_0.output.data))
    pipeline.add_component(intersection_1, data=Data(data=dataio_1.output.data))
    # set train & validate data of hetero_lr_0 component
    pipeline.add_component(hetero_feature_binning_0, data=Data(data=intersection_0.output.data))

    pipeline.add_component(statistic_0, data=Data(data=intersection_0.output.data))
    pipeline.add_component(hetero_feature_selection_0, data=Data(data=intersection_0.output.data),
                           model=Model(isometric_model=[hetero_feature_binning_0.output.model,
                                                        statistic_0.output.model]))
    pipeline.add_component(hetero_feature_selection_1, data=Data(data=intersection_1.output.data),
                           model=Model(hetero_feature_selection_0.output.model))

    pipeline.add_component(hetero_scale_0, data=Data(data=hetero_feature_selection_0.output.data))
    pipeline.add_component(hetero_scale_1, data=Data(data=hetero_feature_selection_1.output.data),
                           model=Model(hetero_scale_0.output.model))

    # set train & validate data of hetero_lr_0 component

    pipeline.add_component(hetero_lr_0, data=Data(train_data=hetero_scale_0.output.data,
                                                  validate_data=hetero_scale_1.output.data))

    pipeline.add_component(evaluation_0, data=Data(data=[hetero_lr_0.output.data]))

    # compile pipeline once finished adding modules, this step will form conf and dsl files for running job
    pipeline.compile()

    # fit model
    pipeline.fit()
    # query component summary
    print(pipeline.get_component("hetero_lr_0").get_summary())


if __name__ == "__main__":
    parser = argparse.ArgumentParser("PIPELINE DEMO")
    parser.add_argument("-config", type=str,
                        help="config file")
    args = parser.parse_args()
    if args.config is not None:
        main(args.config)
    else:
        main()
