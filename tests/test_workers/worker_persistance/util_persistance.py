#SPDX-License-Identifier: MIT
#import pytest

#from tests.test_workers.test_data import *
from tests.test_workers.test_set_up_fixtures import *


#Sample source data generation that pulls json data that has contributions listed
@pytest.fixture
def sample_source_data_enriched():
    with open("tests/test_workers/worker_persistance/contributors.json") as jsonFile:
        source_data = json.load(jsonFile)

    return source_data

#Sample source data generation that opens json data that doesn't have contributions listed
@pytest.fixture
def sample_source_data_unenriched():
    with open("tests/test_workers/worker_persistance/contributors_un_enriched.json") as jsonFile:
        source_data = json.load(jsonFile)

    return source_data

#Bad data that an api might return
@pytest.fixture
def sample_source_data_bad_api_return():
    with open("tests/test_workers/worker_persistance/bad_Data.json") as jsonFile:
        source_data = json.load(jsonFile)

    return source_data


#Sample data for comments api return
@pytest.fixture
def sample_source_data_standard_github_comments():
    with open("tests/test_workers/worker_persistance/standard_enrich_cntrb_id_data.json") as jsonFile:
        source_data = json.load(jsonFile)

    return source_data