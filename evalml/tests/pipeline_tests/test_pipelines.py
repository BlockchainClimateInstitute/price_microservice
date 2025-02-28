import os
import warnings
from unittest.mock import patch

import cloudpickle
import numpy as np
import pandas as pd
import pytest
import woodwork as ww
from pandas.testing import assert_frame_equal
from skopt.space import Integer, Real

from evalml.demos import load_breast_cancer, load_wine
from evalml.exceptions import (
    IllFormattedClassNameError,
    MissingComponentError,
    ObjectiveCreationError,
    ObjectiveNotFoundError,
    PipelineNotYetFittedError,
    PipelineScoreError
)
from evalml.model_family import ModelFamily
from evalml.objectives import (
    CostBenefitMatrix,
    FraudCost,
    Precision,
    get_objective
)
from evalml.pipelines import (
    BinaryClassificationPipeline,
    MulticlassClassificationPipeline,
    PipelineBase,
    RegressionPipeline
)
from evalml.pipelines.components import (
    ElasticNetClassifier,
    Estimator,
    Imputer,
    LogisticRegressionClassifier,
    OneHotEncoder,
    RandomForestClassifier,
    RFClassifierSelectFromModel,
    StandardScaler,
    Transformer
)
from evalml.pipelines.components.utils import (
    _all_estimators_used_in_search,
    allowed_model_families
)
from evalml.pipelines.utils import generate_pipeline_code, get_estimators
from evalml.preprocessing.utils import is_classification
from evalml.problem_types import (
    ProblemTypes,
    is_binary,
    is_multiclass,
    is_time_series
)


def test_allowed_model_families(has_minimal_dependencies):
    families = [ModelFamily.RANDOM_FOREST, ModelFamily.LINEAR_MODEL, ModelFamily.EXTRA_TREES, ModelFamily.DECISION_TREE]
    expected_model_families_binary = set(families)
    expected_model_families_regression = set(families)
    if not has_minimal_dependencies:
        expected_model_families_binary.update([ModelFamily.XGBOOST, ModelFamily.CATBOOST, ModelFamily.LIGHTGBM])
        expected_model_families_regression.update([ModelFamily.CATBOOST, ModelFamily.XGBOOST, ModelFamily.LIGHTGBM])
    assert set(allowed_model_families(ProblemTypes.BINARY)) == expected_model_families_binary
    assert set(allowed_model_families(ProblemTypes.REGRESSION)) == expected_model_families_regression


def test_all_estimators(has_minimal_dependencies):
    if has_minimal_dependencies:
        assert len((_all_estimators_used_in_search())) == 10
    else:
        assert len(_all_estimators_used_in_search()) == 16


def test_get_estimators(has_minimal_dependencies):
    if has_minimal_dependencies:
        assert len(get_estimators(problem_type=ProblemTypes.BINARY)) == 5
        assert len(get_estimators(problem_type=ProblemTypes.BINARY, model_families=[ModelFamily.LINEAR_MODEL])) == 2
        assert len(get_estimators(problem_type=ProblemTypes.MULTICLASS)) == 5
        assert len(get_estimators(problem_type=ProblemTypes.REGRESSION)) == 5
    else:
        assert len(get_estimators(problem_type=ProblemTypes.BINARY)) == 8
        assert len(get_estimators(problem_type=ProblemTypes.BINARY, model_families=[ModelFamily.LINEAR_MODEL])) == 2
        assert len(get_estimators(problem_type=ProblemTypes.MULTICLASS)) == 8
        assert len(get_estimators(problem_type=ProblemTypes.REGRESSION)) == 8

    assert len(get_estimators(problem_type=ProblemTypes.BINARY, model_families=[])) == 0
    assert len(get_estimators(problem_type=ProblemTypes.MULTICLASS, model_families=[])) == 0
    assert len(get_estimators(problem_type=ProblemTypes.REGRESSION, model_families=[])) == 0

    with pytest.raises(RuntimeError, match="Unrecognized model type for problem type"):
        get_estimators(problem_type=ProblemTypes.REGRESSION, model_families=["random_forest", "none"])
    with pytest.raises(TypeError, match="model_families parameter is not a list."):
        get_estimators(problem_type=ProblemTypes.REGRESSION, model_families='random_forest')
    with pytest.raises(KeyError):
        get_estimators(problem_type="Not A Valid Problem Type")


def test_required_fields():
    class TestPipelineWithoutComponentGraph(PipelineBase):
        pass

    with pytest.raises(TypeError):
        TestPipelineWithoutComponentGraph(parameters={})


def test_serialization(X_y_binary, tmpdir, logistic_regression_binary_pipeline_class):
    X, y = X_y_binary
    path = os.path.join(str(tmpdir), 'pipe.pkl')
    pipeline = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    pipeline.fit(X, y)
    pipeline.save(path)
    assert pipeline.score(X, y, ['precision']) == PipelineBase.load(path).score(X, y, ['precision'])


@patch('cloudpickle.dump')
def test_serialization_protocol(mock_cloudpickle_dump, tmpdir, logistic_regression_binary_pipeline_class):
    path = os.path.join(str(tmpdir), 'pipe.pkl')
    pipeline = logistic_regression_binary_pipeline_class(parameters={})

    pipeline.save(path)
    assert len(mock_cloudpickle_dump.call_args_list) == 1
    assert mock_cloudpickle_dump.call_args_list[0][1]['protocol'] == cloudpickle.DEFAULT_PROTOCOL

    mock_cloudpickle_dump.reset_mock()

    pipeline.save(path, pickle_protocol=42)
    assert len(mock_cloudpickle_dump.call_args_list) == 1
    assert mock_cloudpickle_dump.call_args_list[0][1]['protocol'] == 42


@pytest.fixture
def pickled_pipeline_path(X_y_binary, tmpdir, logistic_regression_binary_pipeline_class):
    X, y = X_y_binary
    path = os.path.join(str(tmpdir), 'pickled_pipe.pkl')
    pipeline = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    pipeline.fit(X, y)
    pipeline.save(path)
    return path


def test_load_pickled_pipeline_with_custom_objective(X_y_binary, pickled_pipeline_path, logistic_regression_binary_pipeline_class):
    X, y = X_y_binary
    # checks that class is not defined before loading in pipeline
    with pytest.raises(NameError):
        MockPrecision()  # noqa: F821: ignore flake8's "undefined name" error
    objective = Precision()
    pipeline = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    pipeline.fit(X, y)
    assert PipelineBase.load(pickled_pipeline_path).score(X, y, [objective]) == pipeline.score(X, y, [objective])


def test_reproducibility(X_y_binary, logistic_regression_binary_pipeline_class):
    X, y = X_y_binary
    objective = FraudCost(
        retry_percentage=.5,
        interchange_fee=.02,
        fraud_payout_percentage=.75,
        amount_col=10
    )

    parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "mean",
        },
        'Logistic Regression Classifier': {
            'penalty': 'l2',
            'C': 1.0,
            'n_jobs': 1
        }
    }

    clf = logistic_regression_binary_pipeline_class(parameters=parameters)
    clf.fit(X, y)

    clf_1 = logistic_regression_binary_pipeline_class(parameters=parameters)
    clf_1.fit(X, y)

    assert clf_1.score(X, y, [objective]) == clf.score(X, y, [objective])


def test_indexing(X_y_binary, logistic_regression_binary_pipeline_class):
    X, y = X_y_binary
    clf = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    clf.fit(X, y)

    assert isinstance(clf[1], OneHotEncoder)
    assert isinstance(clf['Imputer'], Imputer)

    setting_err_msg = 'Setting pipeline components is not supported.'
    with pytest.raises(NotImplementedError, match=setting_err_msg):
        clf[1] = OneHotEncoder()

    slicing_err_msg = 'Slicing pipelines is currently not supported.'
    with pytest.raises(NotImplementedError, match=slicing_err_msg):
        clf[:1]


@pytest.mark.parametrize("is_linear", [True, False])
@pytest.mark.parametrize("is_fitted", [True, False])
@pytest.mark.parametrize("return_dict", [True, False])
def test_describe_pipeline(is_linear, is_fitted, return_dict,
                           X_y_binary, caplog, logistic_regression_binary_pipeline_class, nonlinear_binary_pipeline_class):
    X, y = X_y_binary

    if is_linear:
        pipeline = logistic_regression_binary_pipeline_class(parameters={})
        name = "Logistic Regression Binary Pipeline"
        expected_pipeline_dict = {'name': 'Logistic Regression Binary Pipeline',
                                  'problem_type': ProblemTypes.BINARY,
                                  'model_family': ModelFamily.LINEAR_MODEL,
                                  'components': {'Imputer': {'name': 'Imputer', 'parameters': {'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': None, 'numeric_fill_value': None}},
                                                 'One Hot Encoder': {'name': 'One Hot Encoder', 'parameters': {'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}},
                                                 'Standard Scaler': {'name': 'Standard Scaler', 'parameters': {}},
                                                 'Logistic Regression Classifier': {'name': 'Logistic Regression Classifier', 'parameters': {'penalty': 'l2', 'C': 1.0, 'n_jobs': -1, 'multi_class': 'auto', 'solver': 'lbfgs'}}}}
    else:
        pipeline = nonlinear_binary_pipeline_class(parameters={})
        name = "Non Linear Binary Pipeline"
        expected_pipeline_dict = {
            'name': 'Non Linear Binary Pipeline',
            'problem_type': ProblemTypes.BINARY,
            'model_family': ModelFamily.LINEAR_MODEL,
            'components': {'Imputer': {'name': 'Imputer', 'parameters': {'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': None, 'numeric_fill_value': None}},
                           'One Hot Encoder': {'name': 'One Hot Encoder', 'parameters': {'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}},
                           'Elastic Net Classifier': {'name': 'Elastic Net Classifier', 'parameters': {'alpha': 0.5, 'l1_ratio': 0.5, 'n_jobs': -1, 'max_iter': 1000, 'penalty': 'elasticnet', 'loss': 'log'}},
                           'Random Forest Classifier': {'name': 'Random Forest Classifier', 'parameters': {'n_estimators': 100, 'max_depth': 6, 'n_jobs': -1}},
                           'Logistic Regression Classifier': {'name': 'Logistic Regression Classifier', 'parameters': {'penalty': 'l2', 'C': 1.0, 'n_jobs': -1, 'multi_class': 'auto', 'solver': 'lbfgs'}}}
        }

    if is_fitted:
        pipeline.fit(X, y)

    pipeline_dict = pipeline.describe(return_dict=return_dict)
    if return_dict:
        assert pipeline_dict == expected_pipeline_dict
    else:
        assert pipeline_dict is None

    out = caplog.text
    assert name in out
    assert "Problem Type: binary" in out
    assert "Model Family: Linear" in out

    if is_fitted:
        assert "Number of features: " in out
    else:
        assert "Number of features: " not in out

    for component in pipeline:
        if component.hyperparameter_ranges:
            for parameter in component.hyperparameter_ranges:
                assert parameter in out
        assert component.name in out


def test_nonlinear_model_family():
    class DummyNonlinearPipeline(BinaryClassificationPipeline):
        component_graph = {'Imputer': ['Imputer'],
                           'OneHot': ['One Hot Encoder', 'Imputer.x'],
                           'Elastic Net': ['Elastic Net Classifier', 'OneHot.x'],
                           'Logistic Regression': ['Logistic Regression Classifier', 'OneHot.x'],
                           'Random Forest': ['Random Forest Classifier', 'Logistic Regression', 'Elastic Net']}

    class DummyTransformerEndPipeline(BinaryClassificationPipeline):
        component_graph = {'Imputer': ['Imputer'],
                           'OneHot': ['One Hot Encoder', 'Imputer.x'],
                           'Random Forest': ['Random Forest Classifier', 'OneHot.x'],
                           'Logistic Regression': ['Logistic Regression Classifier', 'OneHot.x'],
                           'Scaler': ['Standard Scaler', 'Random Forest', 'Logistic Regression']}

    assert DummyNonlinearPipeline.model_family == ModelFamily.RANDOM_FOREST
    assert DummyTransformerEndPipeline.model_family == ModelFamily.NONE

    nlbp = DummyNonlinearPipeline({})
    nltp = DummyTransformerEndPipeline({})

    assert nlbp.model_family == ModelFamily.RANDOM_FOREST
    assert nltp.model_family == ModelFamily.NONE


def test_parameters(logistic_regression_binary_pipeline_class):
    parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "median"
        },
        'Logistic Regression Classifier': {
            'penalty': 'l2',
            'C': 3.0,
        }
    }
    lrp = logistic_regression_binary_pipeline_class(parameters=parameters)
    expected_parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "median",
            'categorical_fill_value': None,
            'numeric_fill_value': None
        },
        'One Hot Encoder': {
            'top_n': 10,
            'features_to_encode': None,
            'categories': None,
            'drop': None,
            'handle_unknown': 'ignore',
            'handle_missing': 'error'
        },
        'Logistic Regression Classifier': {
            'penalty': 'l2',
            'C': 3.0,
            'n_jobs': -1,
            'multi_class': 'auto',
            'solver': 'lbfgs'
        }
    }
    assert lrp.parameters == expected_parameters


def test_parameters_nonlinear(nonlinear_binary_pipeline_class):

    parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "median"
        },
        'Logistic Regression': {
            'penalty': 'l2',
            'C': 3.0,
        }
    }
    nlbp = nonlinear_binary_pipeline_class(parameters=parameters)
    expected_parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "median",
            'categorical_fill_value': None,
            'numeric_fill_value': None
        },
        'OneHot_RandomForest': {
            'top_n': 10,
            'features_to_encode': None,
            'categories': None,
            'drop': None,
            'handle_unknown': 'ignore',
            'handle_missing': 'error'
        },
        'OneHot_ElasticNet': {
            'top_n': 10,
            'features_to_encode': None,
            'categories': None,
            'drop': None,
            'handle_unknown': 'ignore',
            'handle_missing': 'error'
        },
        'Random Forest': {
            'max_depth': 6,
            'n_estimators': 100,
            'n_jobs': -1
        },
        'Elastic Net': {
            'alpha': 0.5,
            'l1_ratio': 0.5,
            'loss': 'log',
            'max_iter': 1000,
            'n_jobs': -1,
            'penalty': 'elasticnet'
        },
        'Logistic Regression': {
            'penalty': 'l2',
            'C': 3.0,
            'n_jobs': -1,
            'multi_class': 'auto',
            'solver': 'lbfgs'
        }
    }
    assert nlbp.parameters == expected_parameters


def test_name():
    class TestNamePipeline(BinaryClassificationPipeline):
        component_graph = ['Logistic Regression Classifier']

    class TestDefinedNamePipeline(BinaryClassificationPipeline):
        custom_name = "Cool Logistic Regression"
        component_graph = ['Logistic Regression Classifier']

    class testillformattednamepipeline(BinaryClassificationPipeline):
        component_graph = ['Logistic Regression Classifier']

    assert TestNamePipeline.name == "Test Name Pipeline"
    assert TestNamePipeline.custom_name is None
    assert TestDefinedNamePipeline.name == "Cool Logistic Regression"
    assert TestDefinedNamePipeline.custom_name == "Cool Logistic Regression"
    assert TestDefinedNamePipeline(parameters={}).name == "Cool Logistic Regression"
    with pytest.raises(IllFormattedClassNameError):
        testillformattednamepipeline.name == "Test Illformatted Name Pipeline"


def test_multi_format_creation(X_y_binary):
    X, y = X_y_binary

    class TestPipeline(BinaryClassificationPipeline):
        component_graph = component_graph = ['Imputer', 'One Hot Encoder', StandardScaler, 'Logistic Regression Classifier']

        hyperparameters = {
            'Imputer': {
                "categorical_impute_strategy": ["most_frequent"],
                "numeric_impute_strategy": ["mean", "median", "most_frequent"]
            },
            'Logistic Regression Classifier': {
                "penalty": ["l2"],
                "C": Real(.01, 10)
            }
        }

    parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "mean",
        },
        'Logistic Regression Classifier': {
            'penalty': 'l2',
            'C': 1.0,
            'n_jobs': 1
        }
    }

    clf = TestPipeline(parameters=parameters)
    correct_components = [Imputer, OneHotEncoder, StandardScaler, LogisticRegressionClassifier]
    for component, correct_components in zip(clf, correct_components):
        assert isinstance(component, correct_components)
    assert clf.model_family == ModelFamily.LINEAR_MODEL

    clf.fit(X, y)
    clf.score(X, y, ['precision'])
    assert not clf.feature_importance.isnull().all().all()


def test_multiple_feature_selectors(X_y_binary):
    X, y = X_y_binary

    class TestPipeline(BinaryClassificationPipeline):
        component_graph = ['Imputer', 'One Hot Encoder', 'RF Classifier Select From Model', StandardScaler, 'RF Classifier Select From Model', 'Logistic Regression Classifier']

        hyperparameters = {
            'Imputer': {
                "categorical_impute_strategy": ["most_frequent"],
                "numeric_impute_strategy": ["mean", "median", "most_frequent"]
            },
            'Logistic Regression Classifier': {
                "penalty": ["l2"],
                "C": Real(.01, 10)
            }
        }

    clf = TestPipeline(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    correct_components = [Imputer, OneHotEncoder, RFClassifierSelectFromModel, StandardScaler, RFClassifierSelectFromModel, LogisticRegressionClassifier]
    for component, correct_components in zip(clf, correct_components):
        assert isinstance(component, correct_components)
    assert clf.model_family == ModelFamily.LINEAR_MODEL

    clf.fit(X, y)
    clf.score(X, y, ['precision'])
    assert not clf.feature_importance.isnull().all().all()


def test_problem_types():
    class TestPipeline(BinaryClassificationPipeline):
        component_graph = ['Random Forest Regressor']

    with pytest.raises(ValueError, match="not valid for this component graph. Valid problem types include *."):
        TestPipeline(parameters={})


def make_mock_regression_pipeline():
    class MockRegressionPipeline(RegressionPipeline):
        component_graph = ['Random Forest Regressor']

    return MockRegressionPipeline({})


def make_mock_binary_pipeline():
    class MockBinaryClassificationPipeline(BinaryClassificationPipeline):
        component_graph = ['Random Forest Classifier']

    return MockBinaryClassificationPipeline({})


def make_mock_multiclass_pipeline():
    class MockMulticlassClassificationPipeline(MulticlassClassificationPipeline):
        component_graph = ['Random Forest Classifier']

    return MockMulticlassClassificationPipeline({})


@patch('evalml.pipelines.RegressionPipeline.fit')
@patch('evalml.pipelines.RegressionPipeline.predict')
def test_score_regression_single(mock_predict, mock_fit, X_y_regression):
    X, y = X_y_regression
    mock_predict.return_value = ww.DataColumn(y)
    clf = make_mock_regression_pipeline()
    clf.fit(X, y)
    objective_names = ['r2']
    scores = clf.score(X, y, objective_names)
    mock_predict.assert_called()
    assert scores == {'R2': 1.0}


@patch('evalml.pipelines.ComponentGraph.fit')
@patch('evalml.pipelines.RegressionPipeline.predict')
def test_score_nonlinear_regression(mock_predict, mock_fit, nonlinear_regression_pipeline_class, X_y_regression):
    X, y = X_y_regression
    mock_predict.return_value = ww.DataColumn(y)
    clf = nonlinear_regression_pipeline_class({})
    clf.fit(X, y)
    objective_names = ['r2']
    scores = clf.score(X, y, objective_names)
    mock_predict.assert_called()
    assert scores == {'R2': 1.0}


@patch('evalml.pipelines.BinaryClassificationPipeline._encode_targets')
@patch('evalml.pipelines.BinaryClassificationPipeline.fit')
@patch('evalml.pipelines.components.Estimator.predict')
def test_score_binary_single(mock_predict, mock_fit, mock_encode, X_y_binary):
    X, y = X_y_binary
    mock_predict.return_value = y
    mock_encode.return_value = y
    clf = make_mock_binary_pipeline()
    clf.fit(X, y)
    objective_names = ['f1']
    scores = clf.score(X, y, objective_names)
    mock_encode.assert_called()
    mock_fit.assert_called()
    mock_predict.assert_called()
    assert scores == {'F1': 1.0}


@patch('evalml.pipelines.MulticlassClassificationPipeline._encode_targets')
@patch('evalml.pipelines.MulticlassClassificationPipeline.fit')
@patch('evalml.pipelines.components.Estimator.predict')
def test_score_multiclass_single(mock_predict, mock_fit, mock_encode, X_y_binary):
    X, y = X_y_binary
    mock_predict.return_value = y
    mock_encode.return_value = y
    clf = make_mock_multiclass_pipeline()
    clf.fit(X, y)
    objective_names = ['f1 micro']
    scores = clf.score(X, y, objective_names)
    mock_encode.assert_called()
    mock_fit.assert_called()
    mock_predict.assert_called()
    assert scores == {'F1 Micro': 1.0}


@patch('evalml.pipelines.MulticlassClassificationPipeline._encode_targets')
@patch('evalml.pipelines.MulticlassClassificationPipeline.fit')
@patch('evalml.pipelines.ComponentGraph.predict')
def test_score_nonlinear_multiclass(mock_predict, mock_fit, mock_encode, nonlinear_multiclass_pipeline_class, X_y_multi):
    X, y = X_y_multi
    mock_predict.return_value = ww.DataColumn(y)
    mock_encode.return_value = y
    clf = nonlinear_multiclass_pipeline_class({})
    clf.fit(X, y)
    objective_names = ['f1 micro', 'precision micro']
    scores = clf.score(X, y, objective_names)
    mock_predict.assert_called()
    assert scores == {'F1 Micro': 1.0, 'Precision Micro': 1.0}


@patch('evalml.pipelines.RegressionPipeline.fit')
@patch('evalml.pipelines.RegressionPipeline.predict')
def test_score_regression_list(mock_predict, mock_fit, X_y_binary):
    X, y = X_y_binary
    mock_predict.return_value = ww.DataColumn(y)
    clf = make_mock_regression_pipeline()
    clf.fit(X, y)
    objective_names = ['r2', 'mse']
    scores = clf.score(X, y, objective_names)
    mock_predict.assert_called()
    assert scores == {'R2': 1.0, 'MSE': 0.0}


@patch('evalml.pipelines.BinaryClassificationPipeline._encode_targets')
@patch('evalml.pipelines.BinaryClassificationPipeline.fit')
@patch('evalml.pipelines.components.Estimator.predict')
def test_score_binary_list(mock_predict, mock_fit, mock_encode, X_y_binary):
    X, y = X_y_binary
    mock_predict.return_value = y
    mock_encode.return_value = y
    clf = make_mock_binary_pipeline()
    clf.fit(X, y)
    objective_names = ['f1', 'precision']
    scores = clf.score(X, y, objective_names)
    mock_fit.assert_called()
    mock_encode.assert_called()
    mock_predict.assert_called()
    assert scores == {'F1': 1.0, 'Precision': 1.0}


@patch('evalml.pipelines.MulticlassClassificationPipeline._encode_targets')
@patch('evalml.pipelines.MulticlassClassificationPipeline.fit')
@patch('evalml.pipelines.components.Estimator.predict')
def test_score_multi_list(mock_predict, mock_fit, mock_encode, X_y_binary):
    X, y = X_y_binary
    mock_predict.return_value = y
    mock_encode.return_value = y
    clf = make_mock_multiclass_pipeline()
    clf.fit(X, y)
    objective_names = ['f1 micro', 'precision micro']
    scores = clf.score(X, y, objective_names)
    mock_predict.assert_called()
    assert scores == {'F1 Micro': 1.0, 'Precision Micro': 1.0}


@patch('evalml.objectives.R2.score')
@patch('evalml.pipelines.RegressionPipeline.fit')
@patch('evalml.pipelines.RegressionPipeline.predict')
def test_score_regression_objective_error(mock_predict, mock_fit, mock_objective_score, X_y_binary):
    mock_objective_score.side_effect = Exception('finna kabooom 💣')
    X, y = X_y_binary
    mock_predict.return_value = ww.DataColumn(y)
    clf = make_mock_regression_pipeline()
    clf.fit(X, y)
    objective_names = ['r2', 'mse']
    # Using pytest.raises to make sure we error if an error is not thrown.
    with pytest.raises(PipelineScoreError):
        _ = clf.score(X, y, objective_names)
    try:
        _ = clf.score(X, y, objective_names)
    except PipelineScoreError as e:
        assert e.scored_successfully == {"MSE": 0.0}
        assert 'finna kabooom 💣' in e.message
        assert "R2" in e.exceptions


@patch('evalml.pipelines.BinaryClassificationPipeline._encode_targets')
@patch('evalml.objectives.F1.score')
@patch('evalml.pipelines.BinaryClassificationPipeline.fit')
@patch('evalml.pipelines.components.Estimator.predict')
def test_score_binary_objective_error(mock_predict, mock_fit, mock_objective_score, mock_encode, X_y_binary):
    mock_objective_score.side_effect = Exception('finna kabooom 💣')
    X, y = X_y_binary
    mock_predict.return_value = y
    mock_encode.return_value = y
    clf = make_mock_binary_pipeline()
    clf.fit(X, y)
    objective_names = ['f1', 'precision']
    # Using pytest.raises to make sure we error if an error is not thrown.
    with pytest.raises(PipelineScoreError):
        _ = clf.score(X, y, objective_names)
    try:
        _ = clf.score(X, y, objective_names)
    except PipelineScoreError as e:
        assert e.scored_successfully == {"Precision": 1.0}
        assert 'finna kabooom 💣' in e.message


@patch('evalml.pipelines.BinaryClassificationPipeline._encode_targets')
@patch('evalml.objectives.F1.score')
@patch('evalml.pipelines.BinaryClassificationPipeline.fit')
@patch('evalml.pipelines.ComponentGraph.predict')
def test_score_nonlinear_binary_objective_error(mock_predict, mock_fit, mock_objective_score, mock_encode, nonlinear_binary_pipeline_class, X_y_binary):
    mock_objective_score.side_effect = Exception('finna kabooom 💣')
    X, y = X_y_binary
    mock_predict.return_value = ww.DataColumn(y)
    mock_encode.return_value = y
    clf = nonlinear_binary_pipeline_class({})
    clf.fit(X, y)
    objective_names = ['f1', 'precision']
    # Using pytest.raises to make sure we error if an error is not thrown.
    with pytest.raises(PipelineScoreError):
        _ = clf.score(X, y, objective_names)
    try:
        _ = clf.score(X, y, objective_names)
    except PipelineScoreError as e:
        assert e.scored_successfully == {"Precision": 1.0}
        assert 'finna kabooom 💣' in e.message


@patch('evalml.pipelines.MulticlassClassificationPipeline._encode_targets')
@patch('evalml.objectives.F1Micro.score')
@patch('evalml.pipelines.MulticlassClassificationPipeline.fit')
@patch('evalml.pipelines.components.Estimator.predict')
def test_score_multiclass_objective_error(mock_predict, mock_fit, mock_objective_score, mock_encode, X_y_binary):
    mock_objective_score.side_effect = Exception('finna kabooom 💣')
    X, y = X_y_binary
    mock_predict.return_value = y
    mock_encode.return_value = y
    clf = make_mock_multiclass_pipeline()
    clf.fit(X, y)
    objective_names = ['f1 micro', 'precision micro']
    # Using pytest.raises to make sure we error if an error is not thrown.
    with pytest.raises(PipelineScoreError):
        _ = clf.score(X, y, objective_names)
    try:
        _ = clf.score(X, y, objective_names)
    except PipelineScoreError as e:
        assert e.scored_successfully == {"Precision Micro": 1.0}
        assert 'finna kabooom 💣' in e.message
        assert "F1 Micro" in e.exceptions


@patch('evalml.pipelines.components.Imputer.transform')
@patch('evalml.pipelines.components.OneHotEncoder.transform')
@patch('evalml.pipelines.components.StandardScaler.transform')
def test_compute_estimator_features(mock_scaler, mock_ohe, mock_imputer, X_y_binary, logistic_regression_binary_pipeline_class):
    X, y = X_y_binary
    X = pd.DataFrame(X)
    X_expected = pd.DataFrame(index=X.index, columns=X.columns).fillna(0)
    mock_imputer.return_value = ww.DataTable(X)
    mock_ohe.return_value = ww.DataTable(X)
    mock_scaler.return_value = ww.DataTable(X_expected)
    X_expected = X_expected.astype("Int64")

    pipeline = logistic_regression_binary_pipeline_class({})
    pipeline.fit(X, y)

    X_t = pipeline.compute_estimator_features(X)
    assert_frame_equal(X_expected, X_t.to_dataframe())
    assert mock_imputer.call_count == 2
    assert mock_ohe.call_count == 2
    assert mock_scaler.call_count == 2


@patch('evalml.pipelines.components.Imputer.transform')
@patch('evalml.pipelines.components.OneHotEncoder.transform')
@patch('evalml.pipelines.components.RandomForestClassifier.predict')
@patch('evalml.pipelines.components.ElasticNetClassifier.predict')
def test_compute_estimator_features_nonlinear(mock_en_predict, mock_rf_predict, mock_ohe, mock_imputer, X_y_binary, nonlinear_binary_pipeline_class):
    X, y = X_y_binary
    mock_imputer.return_value = ww.DataTable(X)
    mock_ohe.return_value = ww.DataTable(X)
    mock_en_predict.return_value = ww.DataColumn(np.ones(X.shape[0]))
    mock_rf_predict.return_value = ww.DataColumn(np.zeros(X.shape[0]))
    X_expected_df = pd.DataFrame({'Random Forest': np.zeros(X.shape[0]), 'Elastic Net': np.ones(X.shape[0])})

    pipeline = nonlinear_binary_pipeline_class({})
    pipeline.fit(X, y)
    X_t = pipeline.compute_estimator_features(X)

    assert_frame_equal(X_expected_df, X_t.to_dataframe())
    assert mock_imputer.call_count == 2
    assert mock_ohe.call_count == 4
    assert mock_en_predict.call_count == 2
    assert mock_rf_predict.call_count == 2


def test_no_default_parameters():
    class MockComponent(Transformer):
        name = "Mock Component"
        hyperparameter_ranges = {
            'a': [0, 1, 2]
        }

        def __init__(self, a, b=1, c='2', random_seed=0):
            self.a = a
            self.b = b
            self.c = c

    class TestPipeline(BinaryClassificationPipeline):
        component_graph = [MockComponent, 'Logistic Regression Classifier']

    with pytest.raises(ValueError, match="Error received when instantiating component *."):
        TestPipeline(parameters={})

    assert TestPipeline(parameters={'Mock Component': {'a': 42}})


def test_init_components_invalid_parameters():
    class TestPipeline(BinaryClassificationPipeline):
        component_graph = ['RF Classifier Select From Model', 'Logistic Regression Classifier']

    parameters = {
        'Logistic Regression Classifier': {
            "cool_parameter": "yes"
        }
    }

    with pytest.raises(ValueError, match="Error received when instantiating component"):
        TestPipeline(parameters=parameters)


def test_correct_parameters(logistic_regression_binary_pipeline_class):
    parameters = {
        'Imputer': {
            'categorical_impute_strategy': 'most_frequent',
            'numeric_impute_strategy': 'mean'
        },
        'Logistic Regression Classifier': {
            'penalty': 'l2',
            'C': 3.0,
        }
    }
    lr_pipeline = logistic_regression_binary_pipeline_class(parameters=parameters)
    assert lr_pipeline.estimator.random_seed == 0
    assert lr_pipeline.estimator.parameters['C'] == 3.0
    assert lr_pipeline['Imputer'].parameters['categorical_impute_strategy'] == 'most_frequent'
    assert lr_pipeline['Imputer'].parameters['numeric_impute_strategy'] == 'mean'


def test_correct_nonlinear_parameters(nonlinear_binary_pipeline_class):
    parameters = {
        'Imputer': {
            'categorical_impute_strategy': 'most_frequent',
            'numeric_impute_strategy': 'mean'
        },
        'OneHot_RandomForest': {
            'top_n': 4
        },
        'Logistic Regression': {
            'penalty': 'l2',
            'C': 3.0,
        }
    }
    nlb_pipeline = nonlinear_binary_pipeline_class(parameters=parameters)
    assert nlb_pipeline.estimator.random_seed == 0
    assert nlb_pipeline.estimator.parameters['C'] == 3.0
    assert nlb_pipeline['Imputer'].parameters['categorical_impute_strategy'] == 'most_frequent'
    assert nlb_pipeline['Imputer'].parameters['numeric_impute_strategy'] == 'mean'
    assert nlb_pipeline['OneHot_RandomForest'].parameters['top_n'] == 4
    assert nlb_pipeline['OneHot_ElasticNet'].parameters['top_n'] == 10


def test_hyperparameters():
    class MockPipeline(BinaryClassificationPipeline):
        component_graph = ['Imputer', 'Random Forest Classifier']

    hyperparameters = {
        'Imputer': {
            "categorical_impute_strategy": ["most_frequent"],
            "numeric_impute_strategy": ["mean", "median", "most_frequent"]
        },
        'Random Forest Classifier': {
            "n_estimators": Integer(10, 1000),
            "max_depth": Integer(1, 10)
        }
    }

    assert MockPipeline.hyperparameters == hyperparameters
    assert MockPipeline(parameters={}).hyperparameters == hyperparameters


def test_nonlinear_hyperparameters(nonlinear_regression_pipeline_class):

    hyperparameters = {
        'Imputer': {
            "categorical_impute_strategy": ["most_frequent"],
            "numeric_impute_strategy": ["mean", "median", "most_frequent"]
        },
        'OneHot': {
        },
        'Random Forest': {
            "n_estimators": Integer(10, 1000),
            "max_depth": Integer(1, 32)
        },
        'Elastic Net': {
            'alpha': Real(0, 1),
            'l1_ratio': Real(0, 1)
        },
        'Linear Regressor': {
            'fit_intercept': [True, False],
            'normalize': [True, False]
        }
    }

    assert nonlinear_regression_pipeline_class.hyperparameters == hyperparameters
    assert nonlinear_regression_pipeline_class(parameters={}).hyperparameters == hyperparameters


def test_hyperparameters_override():
    class MockPipelineOverRide(BinaryClassificationPipeline):
        component_graph = ['Imputer', 'Random Forest Classifier']

        custom_hyperparameters = {
            'Imputer': {
                "categorical_impute_strategy": ["most_frequent"],
                "numeric_impute_strategy": ["median", "most_frequent"]
            },
            'Random Forest Classifier': {
                "n_estimators": [1, 100, 200],
                "max_depth": [5]
            }
        }

    hyperparameters = {
        'Imputer': {
            "categorical_impute_strategy": ["most_frequent"],
            "numeric_impute_strategy": ["median", "most_frequent"]
        },
        'Random Forest Classifier': {
            "n_estimators": [1, 100, 200],
            "max_depth": [5]
        }
    }

    assert MockPipelineOverRide.hyperparameters == hyperparameters
    assert MockPipelineOverRide(parameters={}).hyperparameters == hyperparameters


def test_nonlinear_hyperparameters_override():
    class NonLinearRegressionPipelineOverRide(RegressionPipeline):
        component_graph = {
            'Imputer': ['Imputer'],
            'OneHot': ['One Hot Encoder', 'Imputer.x'],
            'Random Forest': ['Random Forest Regressor', 'OneHot.x'],
            'Elastic Net': ['Elastic Net Regressor', 'OneHot.x'],
            'Linear Regressor': ['Linear Regressor', 'Random Forest', 'Elastic Net']
        }
        custom_hyperparameters = {
            'Imputer': {
                "categorical_impute_strategy": ["most_frequent"],
                "numeric_impute_strategy": ["median", "most_frequent"]
            },
            'Random Forest': {
                "n_estimators": [1, 100, 200],
                "max_depth": [5]
            }
        }

    hyperparameters = {
        'Imputer': {
            "categorical_impute_strategy": ["most_frequent"],
            "numeric_impute_strategy": ["median", "most_frequent"]
        },
        'OneHot': {
        },
        'Random Forest': {
            "n_estimators": [1, 100, 200],
            "max_depth": [5]
        },
        'Elastic Net': {
            'alpha': Real(0, 1),
            'l1_ratio': Real(0, 1)
        },
        'Linear Regressor': {
            'fit_intercept': [True, False],
            'normalize': [True, False]
        }
    }

    assert NonLinearRegressionPipelineOverRide.hyperparameters == hyperparameters
    assert NonLinearRegressionPipelineOverRide(parameters={}).hyperparameters == hyperparameters


def test_hyperparameters_none(dummy_classifier_estimator_class):
    class MockEstimator(Estimator):
        name = "Mock Classifier"
        model_family = ModelFamily.NONE
        supported_problem_types = [ProblemTypes.BINARY, ProblemTypes.MULTICLASS]
        hyperparameter_ranges = {}

        def __init__(self, random_seed=0):
            super().__init__(parameters={}, component_obj=None, random_seed=random_seed)

    class MockPipelineNone(BinaryClassificationPipeline):
        component_graph = [MockEstimator]

    assert MockPipelineNone.component_graph == [MockEstimator]
    assert MockPipelineNone.hyperparameters == {'Mock Classifier': {}}
    assert MockPipelineNone(parameters={}).hyperparameters == {'Mock Classifier': {}}


@patch('evalml.pipelines.components.Estimator.predict')
def test_score_with_objective_that_requires_predict_proba(mock_predict, dummy_regression_pipeline_class, X_y_binary):
    X, y = X_y_binary
    mock_predict.return_value = ww.DataColumn(pd.Series([1] * 100))
    # Using pytest.raises to make sure we error if an error is not thrown.
    with pytest.raises(PipelineScoreError):
        clf = dummy_regression_pipeline_class(parameters={})
        clf.fit(X, y)
        clf.score(X, y, ['precision', 'auc'])
    try:
        clf = dummy_regression_pipeline_class(parameters={})
        clf.fit(X, y)
        clf.score(X, y, ['precision', 'auc'])
    except PipelineScoreError as e:
        assert "Invalid objective AUC specified for problem type regression" in e.message
        assert "Invalid objective Precision specified for problem type regression" in e.message
    mock_predict.assert_called()


def test_score_auc(X_y_binary, logistic_regression_binary_pipeline_class):
    X, y = X_y_binary
    lr_pipeline = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    lr_pipeline.fit(X, y)
    lr_pipeline.score(X, y, ['auc'])


def test_pipeline_summary():
    class MockPipelineWithoutEstimator(PipelineBase):
        component_graph = ["Imputer", "One Hot Encoder"]
    assert MockPipelineWithoutEstimator.summary == "Pipeline w/ Imputer + One Hot Encoder"

    class MockPipelineWithSingleComponent(PipelineBase):
        component_graph = ["Imputer"]
    assert MockPipelineWithSingleComponent.summary == "Pipeline w/ Imputer"

    class MockPipelineWithOnlyAnEstimator(PipelineBase):
        component_graph = ["Random Forest Classifier"]
    assert MockPipelineWithOnlyAnEstimator.summary == "Random Forest Classifier"

    class MockPipelineWithNoComponents(PipelineBase):
        component_graph = []
    assert MockPipelineWithNoComponents.summary == "Empty Pipeline"

    class MockPipeline(PipelineBase):
        component_graph = ["Imputer", "One Hot Encoder", "Random Forest Classifier"]
    assert MockPipeline.summary == "Random Forest Classifier w/ Imputer + One Hot Encoder"


def test_nonlinear_pipeline_summary(nonlinear_binary_pipeline_class, nonlinear_multiclass_pipeline_class, nonlinear_regression_pipeline_class):
    assert nonlinear_binary_pipeline_class.summary == "Logistic Regression Classifier w/ Imputer + One Hot Encoder + One Hot Encoder + Random Forest Classifier + Elastic Net Classifier"
    assert nonlinear_multiclass_pipeline_class.summary == "Logistic Regression Classifier w/ Imputer + One Hot Encoder + One Hot Encoder + Random Forest Classifier + Elastic Net Classifier"
    assert nonlinear_regression_pipeline_class.summary == "Linear Regressor w/ Imputer + One Hot Encoder + Random Forest Regressor + Elastic Net Regressor"


def test_drop_columns_in_pipeline():
    class PipelineWithDropCol(BinaryClassificationPipeline):
        component_graph = ['Drop Columns Transformer', 'Imputer', 'Logistic Regression Classifier']

    parameters = {
        'Drop Columns Transformer': {
            'columns': ["column to drop"]
        },
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "mean"
        },
        'Logistic Regression Classifier': {
            'penalty': 'l2',
            'C': 3.0,
            'n_jobs': 1
        }
    }
    pipeline_with_drop_col = PipelineWithDropCol(parameters=parameters)
    X = pd.DataFrame({"column to drop": [1, 0, 1, 3], "other col": [1, 2, 4, 1]})
    y = pd.Series([1, 0, 1, 0])
    pipeline_with_drop_col.fit(X, y)
    pipeline_with_drop_col.score(X, y, ['auc'])
    assert list(pipeline_with_drop_col.feature_importance["feature"]) == ['other col']


@pytest.mark.parametrize("is_linear", [True, False])
def test_clone_init(is_linear, linear_regression_pipeline_class, nonlinear_regression_pipeline_class):
    if is_linear:
        pipeline_class = linear_regression_pipeline_class
    else:
        pipeline_class = nonlinear_regression_pipeline_class
    parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "mean"
        },
        'Linear Regressor': {
            'fit_intercept': True,
            'normalize': True,
        }
    }
    pipeline = pipeline_class(parameters=parameters, random_seed=42)
    pipeline_clone = pipeline.clone()
    assert pipeline.parameters == pipeline_clone.parameters
    assert pipeline.random_seed == pipeline_clone.random_seed


@pytest.mark.parametrize("is_linear", [True, False])
def test_clone_fitted(is_linear, X_y_binary, logistic_regression_binary_pipeline_class, nonlinear_binary_pipeline_class):
    X, y = X_y_binary
    if is_linear:
        pipeline_class = logistic_regression_binary_pipeline_class
    else:
        pipeline_class = nonlinear_binary_pipeline_class
    pipeline = pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}}, random_seed=42)
    pipeline.fit(X, y)
    X_t = pipeline.predict_proba(X)

    pipeline_clone = pipeline.clone()
    assert pipeline.parameters == pipeline_clone.parameters
    assert pipeline.random_seed == pipeline_clone.random_seed

    with pytest.raises(PipelineNotYetFittedError):
        pipeline_clone.predict(X)
    pipeline_clone.fit(X, y)

    X_t_clone = pipeline_clone.predict_proba(X)
    assert_frame_equal(X_t.to_dataframe(), X_t_clone.to_dataframe())


def test_feature_importance_has_feature_names(X_y_binary, logistic_regression_binary_pipeline_class):
    X, y = X_y_binary
    col_names = ["col_{}".format(i) for i in range(len(X[0]))]
    X = pd.DataFrame(X, columns=col_names)
    parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "mean"
        },
        'RF Classifier Select From Model': {
            "percent_features": 1.0,
            "number_features": len(X.columns),
            "n_estimators": 20
        },
        'Logistic Regression Classifier': {
            'penalty': 'l2',
            'C': 1.0,
            'n_jobs': 1
        }
    }

    clf = logistic_regression_binary_pipeline_class(parameters=parameters)
    clf.fit(X, y)
    assert len(clf.feature_importance) == len(X.columns)
    assert not clf.feature_importance.isnull().all().all()
    assert sorted(clf.feature_importance["feature"]) == sorted(col_names)


def test_nonlinear_feature_importance_has_feature_names(X_y_binary, nonlinear_binary_pipeline_class):
    X, y = X_y_binary
    col_names = ["col_{}".format(i) for i in range(len(X[0]))]
    X = pd.DataFrame(X, columns=col_names)
    parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "mean"
        },
        'Logistic Regression Classifier': {
            'penalty': 'l2',
            'C': 1.0,
            'n_jobs': 1
        }
    }

    clf = nonlinear_binary_pipeline_class(parameters=parameters)
    clf.fit(X, y)
    assert len(clf.feature_importance) == 2
    assert not clf.feature_importance.isnull().all().all()
    assert sorted(clf.feature_importance["feature"]) == ['Elastic Net', 'Random Forest']


@pytest.mark.parametrize("problem_type", [ProblemTypes.BINARY, ProblemTypes.MULTICLASS, ProblemTypes.REGRESSION])
def test_feature_importance_has_feature_names_xgboost(problem_type, has_minimal_dependencies,
                                                      X_y_regression, X_y_binary, X_y_multi):
    # Testing that we store the original feature names since we map to numeric values for XGBoost
    if has_minimal_dependencies:
        pytest.skip("Skipping because XGBoost not installed for minimal dependencies")
    if problem_type == ProblemTypes.REGRESSION:
        class XGBoostPipeline(RegressionPipeline):
            component_graph = ['Simple Imputer', 'XGBoost Regressor']
            model_family = ModelFamily.XGBOOST
        X, y = X_y_regression
    elif problem_type == ProblemTypes.BINARY:
        class XGBoostPipeline(BinaryClassificationPipeline):
            component_graph = ['Simple Imputer', 'XGBoost Classifier']
            model_family = ModelFamily.XGBOOST
        X, y = X_y_binary
    elif problem_type == ProblemTypes.MULTICLASS:
        class XGBoostPipeline(MulticlassClassificationPipeline):
            component_graph = ['Simple Imputer', 'XGBoost Classifier']
            model_family = ModelFamily.XGBOOST
        X, y = X_y_multi

    X = pd.DataFrame(X)
    X = X.rename(columns={col_name: f'<[{col_name}]' for col_name in X.columns.values})
    col_names = X.columns.values
    pipeline = XGBoostPipeline({'XGBoost Classifier': {'nthread': 1}})
    pipeline.fit(X, y)
    assert len(pipeline.feature_importance) == len(X.columns)
    assert not pipeline.feature_importance.isnull().all().all()
    assert sorted(pipeline.feature_importance["feature"]) == sorted(col_names)


def test_component_not_found(X_y_binary, logistic_regression_binary_pipeline_class):
    class FakePipeline(BinaryClassificationPipeline):
        component_graph = ['Imputer', 'One Hot Encoder', 'This Component Does Not Exist', 'Standard Scaler', 'Logistic Regression Classifier']
    with pytest.raises(MissingComponentError, match="was not found"):
        FakePipeline(parameters={})


def test_get_default_parameters(logistic_regression_binary_pipeline_class):
    expected_defaults = {
        'Imputer': {
            'categorical_impute_strategy': 'most_frequent',
            'numeric_impute_strategy': 'mean',
            'categorical_fill_value': None,
            'numeric_fill_value': None
        },
        'One Hot Encoder': {
            'top_n': 10,
            'features_to_encode': None,
            'categories': None,
            'drop': None,
            'handle_unknown': 'ignore',
            'handle_missing': 'error'
        },
        'Logistic Regression Classifier': {
            'penalty': 'l2',
            'C': 1.0,
            'n_jobs': -1,
            'multi_class': 'auto',
            'solver': 'lbfgs'
        }
    }
    assert logistic_regression_binary_pipeline_class.default_parameters == expected_defaults


@pytest.mark.parametrize("data_type", ['li', 'np', 'pd', 'ww'])
@pytest.mark.parametrize("problem_type", [ProblemTypes.BINARY, ProblemTypes.MULTICLASS])
@pytest.mark.parametrize("target_type", ['int16', 'int32', 'int64', 'float16', 'float32', 'float64', 'bool', 'category', 'object', 'Int64', 'boolean'])
def test_targets_data_types_classification_pipelines(data_type, problem_type, target_type, all_binary_pipeline_classes,
                                                     make_data_type, all_multiclass_pipeline_classes, helper_functions):
    if data_type == 'np' and target_type in ['Int64', 'boolean']:
        pytest.skip("Skipping test where data type is numpy and target type is nullable dtype")

    if problem_type == ProblemTypes.BINARY:
        objective = "Log Loss Binary"
        pipeline_classes = all_binary_pipeline_classes
        X, y = load_breast_cancer(return_pandas=True)
        if "bool" in target_type:
            y = y.map({"malignant": False, "benign": True})
    elif problem_type == ProblemTypes.MULTICLASS:
        if "bool" in target_type:
            pytest.skip("Skipping test where problem type is multiclass but target type is boolean")
        objective = "Log Loss Multiclass"
        pipeline_classes = all_multiclass_pipeline_classes
        X, y = load_wine(return_pandas=True)

    # Update target types as necessary
    unique_vals = y.unique()

    if "int" in target_type.lower():
        unique_vals = y.unique()
        y = y.map({unique_vals[i]: int(i) for i in range(len(unique_vals))})
    elif "float" in target_type.lower():
        unique_vals = y.unique()
        y = y.map({unique_vals[i]: float(i) for i in range(len(unique_vals))})
    if target_type == "category":
        y = pd.Categorical(y)
    else:
        y = y.astype(target_type)
    unique_vals = y.unique()

    X = make_data_type(data_type, X)
    y = make_data_type(data_type, y)

    for pipeline_class in pipeline_classes:
        pipeline = helper_functions.safe_init_pipeline_with_njobs_1(pipeline_class)
        pipeline.fit(X, y)
        predictions = pipeline.predict(X, objective).to_series()
        assert set(predictions.unique()).issubset(unique_vals)
        predict_proba = pipeline.predict_proba(X)
        assert set(predict_proba.columns) == set(unique_vals)


@pytest.mark.parametrize("problem_type", [ProblemTypes.BINARY, ProblemTypes.MULTICLASS, ProblemTypes.REGRESSION])
def test_pipeline_not_fitted_error(problem_type, X_y_binary, X_y_multi, X_y_regression,
                                   logistic_regression_binary_pipeline_class,
                                   logistic_regression_multiclass_pipeline_class,
                                   linear_regression_pipeline_class):
    if problem_type == ProblemTypes.BINARY:
        X, y = X_y_binary
        clf = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    elif problem_type == ProblemTypes.MULTICLASS:
        X, y = X_y_multi
        clf = logistic_regression_multiclass_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    elif problem_type == ProblemTypes.REGRESSION:
        X, y = X_y_regression
        clf = linear_regression_pipeline_class(parameters={"Linear Regressor": {"n_jobs": 1}})

    with pytest.raises(PipelineNotYetFittedError):
        clf.predict(X)
    with pytest.raises(PipelineNotYetFittedError):
        clf.feature_importance

    if is_classification(problem_type):
        with pytest.raises(PipelineNotYetFittedError):
            clf.predict_proba(X)

    clf.fit(X, y)

    if is_classification(problem_type):
        to_patch = 'evalml.pipelines.ClassificationPipeline._predict'
        if problem_type == ProblemTypes.BINARY:
            to_patch = 'evalml.pipelines.BinaryClassificationPipeline._predict'
        with patch(to_patch) as mock_predict:
            clf.predict(X)
            mock_predict.assert_called()
            _, kwargs = mock_predict.call_args
            assert kwargs['objective'] is None

            mock_predict.reset_mock()
            clf.predict(X, 'Log Loss Binary')
            mock_predict.assert_called()
            _, kwargs = mock_predict.call_args
            assert kwargs['objective'] is not None

            mock_predict.reset_mock()
            clf.predict(X, objective='Log Loss Binary')
            mock_predict.assert_called()
            _, kwargs = mock_predict.call_args
            assert kwargs['objective'] is not None

        clf.predict_proba(X)
    else:
        clf.predict(X)
    clf.feature_importance


@patch('evalml.pipelines.PipelineBase.fit')
@pytest.mark.parametrize("problem_type", [ProblemTypes.BINARY, ProblemTypes.MULTICLASS, ProblemTypes.REGRESSION])
def test_nonlinear_pipeline_not_fitted_error(mock_fit, problem_type, X_y_binary, X_y_multi, X_y_regression,
                                             nonlinear_binary_pipeline_class,
                                             nonlinear_multiclass_pipeline_class,
                                             nonlinear_regression_pipeline_class):
    if problem_type == ProblemTypes.BINARY:
        X, y = X_y_binary
        clf = nonlinear_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    elif problem_type == ProblemTypes.MULTICLASS:
        X, y = X_y_multi
        clf = nonlinear_multiclass_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    elif problem_type == ProblemTypes.REGRESSION:
        X, y = X_y_regression
        clf = nonlinear_regression_pipeline_class(parameters={"Linear Regressor": {"n_jobs": 1}})

    with pytest.raises(PipelineNotYetFittedError):
        clf.predict(X)
    with pytest.raises(PipelineNotYetFittedError):
        clf.feature_importance

    if problem_type in [ProblemTypes.BINARY, ProblemTypes.MULTICLASS]:
        with pytest.raises(PipelineNotYetFittedError):
            clf.predict_proba(X)

    clf.fit(X, y)
    if problem_type in [ProblemTypes.BINARY, ProblemTypes.MULTICLASS]:
        with patch('evalml.pipelines.ClassificationPipeline.predict') as mock_predict:
            clf.predict(X)
            mock_predict.assert_called()
        with patch('evalml.pipelines.ClassificationPipeline.predict_proba') as mock_predict_proba:
            clf.predict_proba(X)
            mock_predict_proba.assert_called()
    else:
        with patch('evalml.pipelines.RegressionPipeline.predict') as mock_predict:
            clf.predict(X)
            mock_predict.assert_called()
    clf.feature_importance


@pytest.mark.parametrize("pipeline_class", [BinaryClassificationPipeline, MulticlassClassificationPipeline, RegressionPipeline])
def test_pipeline_equality_different_attributes(pipeline_class):
    # Tests that two classes which are equivalent are not equal
    if pipeline_class in [BinaryClassificationPipeline, MulticlassClassificationPipeline]:
        final_estimator = 'Random Forest Classifier'
    else:
        final_estimator = 'Random Forest Regressor'

    class MockPipeline(pipeline_class):
        name = "Mock Pipeline"
        component_graph = ['Imputer', final_estimator]

    class MockPipelineWithADifferentClassName(pipeline_class):
        name = "Mock Pipeline"
        component_graph = ['Imputer', final_estimator]

    assert MockPipeline(parameters={}) != MockPipelineWithADifferentClassName(parameters={})


@pytest.mark.parametrize("pipeline_class", [BinaryClassificationPipeline, MulticlassClassificationPipeline, RegressionPipeline])
def test_pipeline_equality_subclasses(pipeline_class):
    if pipeline_class in [BinaryClassificationPipeline, MulticlassClassificationPipeline]:
        final_estimator = 'Random Forest Classifier'
    else:
        final_estimator = 'Random Forest Regressor'

    class MockPipeline(pipeline_class):
        name = "Mock Pipeline"
        component_graph = ['Imputer', final_estimator]

    class MockPipelineSubclass(MockPipeline):
        pass
    assert MockPipeline(parameters={}) != MockPipelineSubclass(parameters={})


@pytest.mark.parametrize("pipeline_class", [BinaryClassificationPipeline, MulticlassClassificationPipeline, RegressionPipeline])
@patch('evalml.pipelines.ComponentGraph.fit')
def test_pipeline_equality(mock_fit, pipeline_class):
    if pipeline_class in [BinaryClassificationPipeline, MulticlassClassificationPipeline]:
        final_estimator = 'Random Forest Classifier'
    else:
        final_estimator = 'Random Forest Regressor'

    parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "mean",
        }
    }

    different_parameters = {
        'Imputer': {
            "categorical_impute_strategy": "constant",
            "numeric_impute_strategy": "mean",
        }
    }

    class MockPipeline(pipeline_class):
        name = "Mock Pipeline"
        component_graph = ['Imputer', final_estimator]

    # Test self-equality
    mock_pipeline = MockPipeline(parameters={})
    assert mock_pipeline == mock_pipeline

    # Test defaults
    assert MockPipeline(parameters={}) == MockPipeline(parameters={})

    # Test random_seed
    assert MockPipeline(parameters={}, random_seed=10) == MockPipeline(parameters={}, random_seed=10)
    assert MockPipeline(parameters={}, random_seed=10) != MockPipeline(parameters={}, random_seed=0)

    # Test parameters
    assert MockPipeline(parameters=parameters) != MockPipeline(parameters=different_parameters)

    # Test fitted equality
    X = pd.DataFrame({})
    y = pd.Series([])
    mock_pipeline.fit(X, y)
    assert mock_pipeline != MockPipeline(parameters={})

    mock_pipeline_equal = MockPipeline(parameters={})
    mock_pipeline_equal.fit(X, y)
    assert mock_pipeline == mock_pipeline_equal

    # Test fitted equality: same data but different target names are not equal
    mock_pipeline_different_target_name = MockPipeline(parameters={})
    mock_pipeline_different_target_name.fit(X, y=pd.Series([], name="target with a name"))
    assert mock_pipeline != mock_pipeline_different_target_name


@pytest.mark.parametrize("pipeline_class", [BinaryClassificationPipeline, MulticlassClassificationPipeline, RegressionPipeline])
def test_nonlinear_pipeline_equality(pipeline_class):
    if pipeline_class in [BinaryClassificationPipeline, MulticlassClassificationPipeline]:
        final_estimator = 'Random Forest Classifier'
    else:
        final_estimator = 'Random Forest Regressor'

    parameters = {
        'Imputer': {
            "categorical_impute_strategy": "most_frequent",
            "numeric_impute_strategy": "mean",
        },
        'OHE_1': {
            'top_n': 5
        }
    }

    different_parameters = {
        'Imputer': {
            "categorical_impute_strategy": "constant",
            "numeric_impute_strategy": "mean",
        },
        'OHE_2': {
            'top_n': 7,
        }
    }

    class MockPipeline(pipeline_class):
        name = "Mock Pipeline"
        component_graph = {
            'Imputer': ['Imputer'],
            'OHE_1': ['One Hot Encoder', 'Imputer'],
            'OHE_2': ['One Hot Encoder', 'Imputer'],
            'Estimator': [final_estimator, 'OHE_1', 'OHE_2']
        }

        def fit(self, X, y=None):
            return self
    # Test self-equality
    mock_pipeline = MockPipeline(parameters={})
    assert mock_pipeline == mock_pipeline

    # Test defaults
    assert MockPipeline(parameters={}) == MockPipeline(parameters={})

    # Test random_seed
    assert MockPipeline(parameters={}, random_seed=10) == MockPipeline(parameters={}, random_seed=10)
    assert MockPipeline(parameters={}, random_seed=10) != MockPipeline(parameters={}, random_seed=0)

    # Test parameters
    assert MockPipeline(parameters=parameters) != MockPipeline(parameters=different_parameters)

    # Test fitted equality
    X = pd.DataFrame({})
    mock_pipeline.fit(X)
    assert mock_pipeline != MockPipeline(parameters={})

    mock_pipeline_equal = MockPipeline(parameters={})
    mock_pipeline_equal.fit(X)
    assert mock_pipeline == mock_pipeline_equal


@pytest.mark.parametrize("problem_type", [ProblemTypes.BINARY, ProblemTypes.MULTICLASS, ProblemTypes.REGRESSION])
def test_pipeline_equality_different_fitted_data(problem_type, X_y_binary, X_y_multi, X_y_regression,
                                                 linear_regression_pipeline_class,
                                                 logistic_regression_binary_pipeline_class,
                                                 logistic_regression_multiclass_pipeline_class):
    # Test fitted on different data
    if problem_type == ProblemTypes.BINARY:
        pipeline = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
        X, y = X_y_binary
    elif problem_type == ProblemTypes.MULTICLASS:
        pipeline = logistic_regression_multiclass_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
        X, y = X_y_multi
    elif problem_type == ProblemTypes.REGRESSION:
        pipeline = linear_regression_pipeline_class(parameters={"Linear Regressor": {"n_jobs": 1}})
        X, y = X_y_regression

    pipeline_diff_data = pipeline.clone()
    assert pipeline == pipeline_diff_data

    pipeline.fit(X, y)
    # Add new column to data to make it different
    X = np.append(X, np.zeros((len(X), 1)), axis=1)
    pipeline_diff_data.fit(X, y)

    assert pipeline != pipeline_diff_data


def test_pipeline_str():

    class MockBinaryPipeline(BinaryClassificationPipeline):
        name = "Mock Binary Pipeline"
        component_graph = ['Imputer', 'Random Forest Classifier']

    class MockMulticlassPipeline(MulticlassClassificationPipeline):
        name = "Mock Multiclass Pipeline"
        component_graph = ['Imputer', 'Random Forest Classifier']

    class MockRegressionPipeline(RegressionPipeline):
        name = "Mock Regression Pipeline"
        component_graph = ['Imputer', 'Random Forest Regressor']

    binary_pipeline = MockBinaryPipeline(parameters={})
    multiclass_pipeline = MockMulticlassPipeline(parameters={})
    regression_pipeline = MockRegressionPipeline(parameters={})

    assert str(binary_pipeline) == "Mock Binary Pipeline"
    assert str(multiclass_pipeline) == "Mock Multiclass Pipeline"
    assert str(regression_pipeline) == "Mock Regression Pipeline"


@pytest.mark.parametrize("pipeline_class", [BinaryClassificationPipeline, MulticlassClassificationPipeline, RegressionPipeline])
def test_pipeline_repr(pipeline_class):
    if pipeline_class in [BinaryClassificationPipeline, MulticlassClassificationPipeline]:
        final_estimator = 'Random Forest Classifier'
    else:
        final_estimator = 'Random Forest Regressor'

    class MockPipeline(pipeline_class):
        name = "Mock Pipeline"
        component_graph = ['Imputer', final_estimator]

    pipeline = MockPipeline(parameters={})
    expected_repr = f"MockPipeline(parameters={{'Imputer':{{'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': None, 'numeric_fill_value': None}}, '{final_estimator}':{{'n_estimators': 100, 'max_depth': 6, 'n_jobs': -1}},}})"
    assert repr(pipeline) == expected_repr

    pipeline_with_parameters = MockPipeline(parameters={'Imputer': {'numeric_fill_value': 42}})
    expected_repr = f"MockPipeline(parameters={{'Imputer':{{'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': None, 'numeric_fill_value': 42}}, '{final_estimator}':{{'n_estimators': 100, 'max_depth': 6, 'n_jobs': -1}},}})"
    assert repr(pipeline_with_parameters) == expected_repr

    pipeline_with_inf_parameters = MockPipeline(parameters={'Imputer': {'numeric_fill_value': float('inf'), 'categorical_fill_value': np.inf}})
    expected_repr = f"MockPipeline(parameters={{'Imputer':{{'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': float('inf'), 'numeric_fill_value': float('inf')}}, '{final_estimator}':{{'n_estimators': 100, 'max_depth': 6, 'n_jobs': -1}},}})"
    assert repr(pipeline_with_inf_parameters) == expected_repr

    pipeline_with_nan_parameters = MockPipeline(parameters={'Imputer': {'numeric_fill_value': float('nan'), 'categorical_fill_value': np.nan}})
    expected_repr = f"MockPipeline(parameters={{'Imputer':{{'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': np.nan, 'numeric_fill_value': np.nan}}, '{final_estimator}':{{'n_estimators': 100, 'max_depth': 6, 'n_jobs': -1}},}})"
    assert repr(pipeline_with_nan_parameters) == expected_repr


@pytest.mark.parametrize("pipeline_class", [BinaryClassificationPipeline, MulticlassClassificationPipeline, RegressionPipeline])
def test_nonlinear_pipeline_repr(pipeline_class):
    if pipeline_class in [BinaryClassificationPipeline, MulticlassClassificationPipeline]:
        final_estimator = 'Random Forest Classifier'
    else:
        final_estimator = 'Random Forest Regressor'

    class MockPipeline(pipeline_class):
        name = "Mock Pipeline"
        component_graph = {
            'Imputer': ['Imputer'],
            'OHE_1': ['One Hot Encoder', 'Imputer'],
            'OHE_2': ['One Hot Encoder', 'Imputer'],
            'Estimator': [final_estimator, 'OHE_1', 'OHE_2']
        }

    pipeline = MockPipeline(parameters={})
    expected_repr = f"MockPipeline(parameters={{'Imputer':{{'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': None, 'numeric_fill_value': None}}, 'OHE_1':{{'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}}, 'OHE_2':{{'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}}, 'Estimator':{{'n_estimators': 100, 'max_depth': 6, 'n_jobs': -1}},}})"
    assert repr(pipeline) == expected_repr

    pipeline_with_parameters = MockPipeline(parameters={'Imputer': {'numeric_fill_value': 42}})
    expected_repr = f"MockPipeline(parameters={{'Imputer':{{'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': None, 'numeric_fill_value': 42}}, 'OHE_1':{{'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}}, 'OHE_2':{{'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}}, 'Estimator':{{'n_estimators': 100, 'max_depth': 6, 'n_jobs': -1}},}})"
    assert repr(pipeline_with_parameters) == expected_repr

    pipeline_with_inf_parameters = MockPipeline(parameters={'Imputer': {'numeric_fill_value': float('inf'), 'categorical_fill_value': np.inf}})
    expected_repr = f"MockPipeline(parameters={{'Imputer':{{'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': float('inf'), 'numeric_fill_value': float('inf')}}, 'OHE_1':{{'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}}, 'OHE_2':{{'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}}, 'Estimator':{{'n_estimators': 100, 'max_depth': 6, 'n_jobs': -1}},}})"
    assert repr(pipeline_with_inf_parameters) == expected_repr

    pipeline_with_nan_parameters = MockPipeline(parameters={'Imputer': {'numeric_fill_value': float('nan'), 'categorical_fill_value': np.nan}})
    expected_repr = f"MockPipeline(parameters={{'Imputer':{{'categorical_impute_strategy': 'most_frequent', 'numeric_impute_strategy': 'mean', 'categorical_fill_value': np.nan, 'numeric_fill_value': np.nan}}, 'OHE_1':{{'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}}, 'OHE_2':{{'top_n': 10, 'features_to_encode': None, 'categories': None, 'drop': None, 'handle_unknown': 'ignore', 'handle_missing': 'error'}}, 'Estimator':{{'n_estimators': 100, 'max_depth': 6, 'n_jobs': -1}},}})"
    assert repr(pipeline_with_nan_parameters) == expected_repr


def test_generate_code_pipeline_errors():
    class MockBinaryPipeline(BinaryClassificationPipeline):
        name = "Mock Binary Pipeline"
        component_graph = ['Imputer', 'Random Forest Classifier']

    class MockMulticlassPipeline(MulticlassClassificationPipeline):
        name = "Mock Multiclass Pipeline"
        component_graph = ['Imputer', 'Random Forest Classifier']

    class MockRegressionPipeline(RegressionPipeline):
        name = "Mock Regression Pipeline"
        component_graph = ['Imputer', 'Random Forest Regressor']

    with pytest.raises(ValueError, match="Element must be a pipeline instance"):
        generate_pipeline_code(MockBinaryPipeline)

    with pytest.raises(ValueError, match="Element must be a pipeline instance"):
        generate_pipeline_code(MockMulticlassPipeline)

    with pytest.raises(ValueError, match="Element must be a pipeline instance"):
        generate_pipeline_code(MockRegressionPipeline)

    with pytest.raises(ValueError, match="Element must be a pipeline instance"):
        generate_pipeline_code([Imputer])

    with pytest.raises(ValueError, match="Element must be a pipeline instance"):
        generate_pipeline_code([Imputer, LogisticRegressionClassifier])

    with pytest.raises(ValueError, match="Element must be a pipeline instance"):
        generate_pipeline_code([Imputer(), LogisticRegressionClassifier()])


def test_generate_code_pipeline_json_errors():
    class CustomEstimator(Estimator):
        name = "My Custom Estimator"
        hyperparameter_ranges = {}
        supported_problem_types = [ProblemTypes.BINARY, ProblemTypes.MULTICLASS]
        model_family = ModelFamily.NONE

        def __init__(self, random_arg=False, numpy_arg=[], random_seed=0):
            parameters = {'random_arg': random_arg,
                          'numpy_arg': numpy_arg}

            super().__init__(parameters=parameters,
                             component_obj=None,
                             random_seed=random_seed)

    class MockBinaryPipelineTransformer(BinaryClassificationPipeline):
        name = "Mock Binary Pipeline with Transformer"
        component_graph = ['Imputer', CustomEstimator]

    pipeline = MockBinaryPipelineTransformer({})
    generate_pipeline_code(pipeline)

    pipeline = MockBinaryPipelineTransformer({'My Custom Estimator': {'numpy_arg': np.array([0])}})
    with pytest.raises(TypeError, match="cannot be JSON-serialized"):
        generate_pipeline_code(pipeline)

    pipeline = MockBinaryPipelineTransformer({'My Custom Estimator': {'random_arg': pd.DataFrame()}})
    with pytest.raises(TypeError, match="cannot be JSON-serialized"):
        generate_pipeline_code(pipeline)

    pipeline = MockBinaryPipelineTransformer({'My Custom Estimator': {'random_arg': ProblemTypes.BINARY}})
    with pytest.raises(TypeError, match="cannot be JSON-serialized"):
        generate_pipeline_code(pipeline)

    pipeline = MockBinaryPipelineTransformer({'My Custom Estimator': {'random_arg': BinaryClassificationPipeline}})
    with pytest.raises(TypeError, match="cannot be JSON-serialized"):
        generate_pipeline_code(pipeline)

    pipeline = MockBinaryPipelineTransformer({'My Custom Estimator': {'random_arg': Estimator}})
    with pytest.raises(TypeError, match="cannot be JSON-serialized"):
        generate_pipeline_code(pipeline)

    pipeline = MockBinaryPipelineTransformer({'My Custom Estimator': {'random_arg': Imputer()}})
    with pytest.raises(TypeError, match="cannot be JSON-serialized"):
        generate_pipeline_code(pipeline)


def test_generate_code_pipeline():
    class MockBinaryPipeline(BinaryClassificationPipeline):
        component_graph = ['Imputer', 'Random Forest Classifier']
        custom_hyperparameters = {
            "Imputer": {
                "numeric_impute_strategy": 'most_frequent'
            }
        }

    class MockRegressionPipeline(RegressionPipeline):
        name = "Mock Regression Pipeline"
        component_graph = ['Imputer', 'Random Forest Regressor']

    mock_binary_pipeline = MockBinaryPipeline({})
    expected_code = 'import json\n' \
                    'from evalml.pipelines.binary_classification_pipeline import BinaryClassificationPipeline' \
                    '\n\nclass MockBinaryPipeline(BinaryClassificationPipeline):' \
                    '\n\tcomponent_graph = [\n\t\t\'Imputer\',\n\t\t\'Random Forest Classifier\'\n\t]' \
                    '\n\tcustom_hyperparameters = {\'Imputer\': {\'numeric_impute_strategy\': \'most_frequent\'}}\n' \
                    '\nparameters = json.loads("""{\n\t"Imputer": {\n\t\t"categorical_impute_strategy": "most_frequent",\n\t\t"numeric_impute_strategy": "mean",\n\t\t"categorical_fill_value": null,\n\t\t"numeric_fill_value": null\n\t},' \
                    '\n\t"Random Forest Classifier": {\n\t\t"n_estimators": 100,\n\t\t"max_depth": 6,\n\t\t"n_jobs": -1\n\t}\n}""")\n' \
                    'pipeline = MockBinaryPipeline(parameters)'
    pipeline = generate_pipeline_code(mock_binary_pipeline)
    assert expected_code == pipeline

    mock_regression_pipeline = MockRegressionPipeline({})
    expected_code = 'import json\n' \
                    'from evalml.pipelines.regression_pipeline import RegressionPipeline' \
                    '\n\nclass MockRegressionPipeline(RegressionPipeline):' \
                    '\n\tcomponent_graph = [\n\t\t\'Imputer\',\n\t\t\'Random Forest Regressor\'\n\t]\n\t' \
                    'name = \'Mock Regression Pipeline\'\n\n' \
                    'parameters = json.loads("""{\n\t"Imputer": {\n\t\t"categorical_impute_strategy": "most_frequent",\n\t\t"numeric_impute_strategy": "mean",\n\t\t"categorical_fill_value": null,\n\t\t"numeric_fill_value": null\n\t},' \
                    '\n\t"Random Forest Regressor": {\n\t\t"n_estimators": 100,\n\t\t"max_depth": 6,\n\t\t"n_jobs": -1\n\t}\n}""")' \
                    '\npipeline = MockRegressionPipeline(parameters)'
    pipeline = generate_pipeline_code(mock_regression_pipeline)
    assert pipeline == expected_code

    mock_regression_pipeline_params = MockRegressionPipeline({"Imputer": {"numeric_impute_strategy": "most_frequent"}, "Random Forest Regressor": {"n_estimators": 50}})
    expected_code_params = 'import json\n' \
                           'from evalml.pipelines.regression_pipeline import RegressionPipeline' \
                           '\n\nclass MockRegressionPipeline(RegressionPipeline):' \
                           '\n\tcomponent_graph = [\n\t\t\'Imputer\',\n\t\t\'Random Forest Regressor\'\n\t]' \
                           '\n\tname = \'Mock Regression Pipeline\'' \
                           '\n\nparameters = json.loads("""{\n\t"Imputer": {\n\t\t"categorical_impute_strategy": "most_frequent",\n\t\t"numeric_impute_strategy": "most_frequent",\n\t\t"categorical_fill_value": null,\n\t\t"numeric_fill_value": null\n\t},' \
                           '\n\t"Random Forest Regressor": {\n\t\t"n_estimators": 50,\n\t\t"max_depth": 6,\n\t\t"n_jobs": -1\n\t}\n}""")' \
                           '\npipeline = MockRegressionPipeline(parameters)'
    pipeline = generate_pipeline_code(mock_regression_pipeline_params)
    assert pipeline == expected_code_params


def test_generate_code_nonlinear_pipeline_error(nonlinear_binary_pipeline_class):
    pipeline = nonlinear_binary_pipeline_class({})
    with pytest.raises(ValueError, match="Code generation for nonlinear pipelines is not supported yet"):
        generate_pipeline_code(pipeline)


def test_generate_code_pipeline_custom():
    class CustomTransformer(Transformer):
        name = "My Custom Transformer"
        hyperparameter_ranges = {}

        def __init__(self, random_seed=0):
            parameters = {}

            super().__init__(parameters=parameters,
                             component_obj=None,
                             random_seed=random_seed)

    class CustomEstimator(Estimator):
        name = "My Custom Estimator"
        hyperparameter_ranges = {}
        supported_problem_types = [ProblemTypes.BINARY, ProblemTypes.MULTICLASS]
        model_family = ModelFamily.NONE

        def __init__(self, random_arg=False, random_seed=0):
            parameters = {'random_arg': random_arg}

            super().__init__(parameters=parameters,
                             component_obj=None,
                             random_seed=random_seed)

    class MockBinaryPipelineTransformer(BinaryClassificationPipeline):
        name = "Mock Binary Pipeline with Transformer"
        component_graph = [CustomTransformer, 'Random Forest Classifier']

    class MockBinaryPipelineEstimator(BinaryClassificationPipeline):
        name = "Mock Binary Pipeline with Estimator"
        component_graph = ['Imputer', CustomEstimator]
        custom_hyperparameters = {
            'Imputer': {
                'numeric_impute_strategy': 'most_frequent'
            }
        }

    class MockAllCustom(BinaryClassificationPipeline):
        name = "Mock All Custom Pipeline"
        component_graph = [CustomTransformer, CustomEstimator]

    mockBinaryTransformer = MockBinaryPipelineTransformer({})
    expected_code = 'import json\n' \
                    'from evalml.pipelines.binary_classification_pipeline import BinaryClassificationPipeline' \
                    '\n\nclass MockBinaryPipelineTransformer(BinaryClassificationPipeline):' \
                    '\n\tcomponent_graph = [\n\t\tCustomTransformer,\n\t\t\'Random Forest Classifier\'\n\t]' \
                    '\n\tname = \'Mock Binary Pipeline with Transformer\'' \
                    '\n\nparameters = json.loads("""{\n\t"Random Forest Classifier": {\n\t\t"n_estimators": 100,\n\t\t"max_depth": 6,\n\t\t"n_jobs": -1\n\t}\n}""")' \
                    '\npipeline = MockBinaryPipelineTransformer(parameters)'
    pipeline = generate_pipeline_code(mockBinaryTransformer)
    assert pipeline == expected_code

    mockBinaryPipeline = MockBinaryPipelineEstimator({})
    expected_code = 'import json\n' \
                    'from evalml.pipelines.binary_classification_pipeline import BinaryClassificationPipeline' \
                    '\n\nclass MockBinaryPipelineEstimator(BinaryClassificationPipeline):' \
                    '\n\tcomponent_graph = [\n\t\t\'Imputer\',\n\t\tCustomEstimator\n\t]' \
                    '\n\tcustom_hyperparameters = {\'Imputer\': {\'numeric_impute_strategy\': \'most_frequent\'}}' \
                    '\n\tname = \'Mock Binary Pipeline with Estimator\'' \
                    '\n\nparameters = json.loads("""{\n\t"Imputer": {\n\t\t"categorical_impute_strategy": "most_frequent",\n\t\t"numeric_impute_strategy": "mean",\n\t\t"categorical_fill_value": null,\n\t\t"numeric_fill_value": null\n\t},' \
                    '\n\t"My Custom Estimator": {\n\t\t"random_arg": false\n\t}\n}""")' \
                    '\npipeline = MockBinaryPipelineEstimator(parameters)'
    pipeline = generate_pipeline_code(mockBinaryPipeline)
    assert pipeline == expected_code

    mockAllCustom = MockAllCustom({})
    expected_code = 'import json\n' \
                    'from evalml.pipelines.binary_classification_pipeline import BinaryClassificationPipeline' \
                    '\n\nclass MockAllCustom(BinaryClassificationPipeline):' \
                    '\n\tcomponent_graph = [\n\t\tCustomTransformer,\n\t\tCustomEstimator\n\t]' \
                    '\n\tname = \'Mock All Custom Pipeline\'\n\nparameters = json.loads("""{\n\t"My Custom Estimator": {\n\t\t"random_arg": false\n\t}\n}""")' \
                    '\npipeline = MockAllCustom(parameters)'
    pipeline = generate_pipeline_code(mockAllCustom)
    assert pipeline == expected_code


@pytest.mark.parametrize("problem_type", [ProblemTypes.BINARY, ProblemTypes.MULTICLASS, ProblemTypes.REGRESSION,
                                          ProblemTypes.TIME_SERIES_REGRESSION, ProblemTypes.TIME_SERIES_BINARY, ProblemTypes.TIME_SERIES_MULTICLASS])
def test_predict_has_input_target_name(problem_type, X_y_binary, X_y_multi, X_y_regression, ts_data,
                                       logistic_regression_binary_pipeline_class, logistic_regression_multiclass_pipeline_class, linear_regression_pipeline_class, time_series_regression_pipeline_class, time_series_binary_classification_pipeline_class,
                                       time_series_multiclass_classification_pipeline_class):
    if problem_type == ProblemTypes.BINARY:
        X, y = X_y_binary
        clf = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})

    elif problem_type == ProblemTypes.MULTICLASS:
        X, y = X_y_multi
        clf = logistic_regression_multiclass_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})

    elif problem_type == ProblemTypes.REGRESSION:
        X, y = X_y_regression
        clf = linear_regression_pipeline_class(parameters={"Linear Regressor": {"n_jobs": 1}})

    elif problem_type == ProblemTypes.TIME_SERIES_REGRESSION:
        X, y = ts_data
        clf = time_series_regression_pipeline_class(parameters={"pipeline": {"gap": 0, "max_delay": 0}})
    elif problem_type == ProblemTypes.TIME_SERIES_BINARY:
        X, y = X_y_binary
        clf = time_series_binary_classification_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1},
                                                                           "pipeline": {"gap": 0, "max_delay": 0}})
    elif problem_type == ProblemTypes.TIME_SERIES_MULTICLASS:
        X, y = X_y_multi
        clf = time_series_multiclass_classification_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1},
                                                                               "pipeline": {"gap": 0, "max_delay": 0}})
    y = pd.Series(y, name="test target name")
    clf.fit(X, y)
    if is_time_series(problem_type):
        predictions = clf.predict(X, y)
    else:
        predictions = clf.predict(X)
    assert predictions.name == "test target name"


def test_linear_pipeline_iteration(logistic_regression_binary_pipeline_class):
    expected_order = [Imputer(), OneHotEncoder(), StandardScaler(), LogisticRegressionClassifier()]

    pipeline = logistic_regression_binary_pipeline_class({})
    order = [c for c in pipeline]
    order_again = [c for c in pipeline]

    assert order == expected_order
    assert order_again == expected_order

    expected_order_params = [Imputer(numeric_impute_strategy='median'), OneHotEncoder(top_n=2), StandardScaler(), LogisticRegressionClassifier()]

    pipeline = logistic_regression_binary_pipeline_class({'One Hot Encoder': {'top_n': 2}, 'Imputer': {'numeric_impute_strategy': 'median'}})
    order_params = [c for c in pipeline]
    order_again_params = [c for c in pipeline]

    assert order_params == expected_order_params
    assert order_again_params == expected_order_params


def test_nonlinear_pipeline_iteration(nonlinear_binary_pipeline_class):
    expected_order = [Imputer(), OneHotEncoder(), ElasticNetClassifier(), OneHotEncoder(), RandomForestClassifier(), LogisticRegressionClassifier()]

    pipeline = nonlinear_binary_pipeline_class({})
    order = [c for c in pipeline]
    order_again = [c for c in pipeline]

    assert order == expected_order
    assert order_again == expected_order

    expected_order_params = [Imputer(), OneHotEncoder(top_n=2), ElasticNetClassifier(), OneHotEncoder(top_n=5), RandomForestClassifier(), LogisticRegressionClassifier()]

    pipeline = nonlinear_binary_pipeline_class({'OneHot_ElasticNet': {'top_n': 2}, 'OneHot_RandomForest': {'top_n': 5}})
    order_params = [c for c in pipeline]
    order_again_params = [c for c in pipeline]

    assert order_params == expected_order_params
    assert order_again_params == expected_order_params


def test_linear_getitem(logistic_regression_binary_pipeline_class):
    pipeline = logistic_regression_binary_pipeline_class({'One Hot Encoder': {'top_n': 4}})

    assert pipeline[0] == Imputer()
    assert pipeline[1] == OneHotEncoder(top_n=4)
    assert pipeline[2] == StandardScaler()
    assert pipeline[3] == LogisticRegressionClassifier()

    assert pipeline['Imputer'] == Imputer()
    assert pipeline['One Hot Encoder'] == OneHotEncoder(top_n=4)
    assert pipeline['Standard Scaler'] == StandardScaler()
    assert pipeline['Logistic Regression Classifier'] == LogisticRegressionClassifier()


def test_nonlinear_getitem(nonlinear_binary_pipeline_class):
    pipeline = nonlinear_binary_pipeline_class({'OneHot_RandomForest': {'top_n': 4}})

    assert pipeline[0] == Imputer()
    assert pipeline[1] == OneHotEncoder()
    assert pipeline[2] == ElasticNetClassifier()
    assert pipeline[3] == OneHotEncoder(top_n=4)
    assert pipeline[4] == RandomForestClassifier()
    assert pipeline[5] == LogisticRegressionClassifier()

    assert pipeline['Imputer'] == Imputer()
    assert pipeline['OneHot_ElasticNet'] == OneHotEncoder()
    assert pipeline['Elastic Net'] == ElasticNetClassifier()
    assert pipeline['OneHot_RandomForest'] == OneHotEncoder(top_n=4)
    assert pipeline['Random Forest'] == RandomForestClassifier()
    assert pipeline['Logistic Regression'] == LogisticRegressionClassifier()


def test_get_component(logistic_regression_binary_pipeline_class, nonlinear_binary_pipeline_class):
    pipeline = logistic_regression_binary_pipeline_class({'One Hot Encoder': {'top_n': 4}})

    assert pipeline.get_component('Imputer') == Imputer()
    assert pipeline.get_component('One Hot Encoder') == OneHotEncoder(top_n=4)
    assert pipeline.get_component('Standard Scaler') == StandardScaler()
    assert pipeline.get_component('Logistic Regression Classifier') == LogisticRegressionClassifier()

    pipeline = nonlinear_binary_pipeline_class({'OneHot_RandomForest': {'top_n': 4}})

    assert pipeline.get_component('Imputer') == Imputer()
    assert pipeline.get_component('OneHot_ElasticNet') == OneHotEncoder()
    assert pipeline.get_component('Elastic Net') == ElasticNetClassifier()
    assert pipeline.get_component('OneHot_RandomForest') == OneHotEncoder(top_n=4)
    assert pipeline.get_component('Random Forest') == RandomForestClassifier()
    assert pipeline.get_component('Logistic Regression') == LogisticRegressionClassifier()


def test_pipelines_raise_deprecated_random_state_warning(dummy_binary_pipeline_class,
                                                         dummy_multiclass_pipeline_class,
                                                         dummy_regression_pipeline_class,
                                                         dummy_time_series_regression_pipeline_class,
                                                         dummy_ts_binary_pipeline_class,
                                                         time_series_multiclass_classification_pipeline_class):
    def test_pipeline_class(pipeline_class):
        with warnings.catch_warnings(record=True) as warn:
            warnings.simplefilter("always")
            pipeline = pipeline_class({"pipeline": {"gap": 3, "max_delay": 2}}, random_state=31)
            assert pipeline.random_seed == 31
            assert str(warn[0].message).startswith("Argument 'random_state' has been deprecated in favor of 'random_seed'")

    test_pipeline_class(dummy_binary_pipeline_class)
    test_pipeline_class(dummy_multiclass_pipeline_class)
    test_pipeline_class(dummy_regression_pipeline_class)
    test_pipeline_class(dummy_time_series_regression_pipeline_class)
    test_pipeline_class(dummy_ts_binary_pipeline_class)
    test_pipeline_class(time_series_multiclass_classification_pipeline_class)


@pytest.mark.parametrize("problem_type", ProblemTypes.all_problem_types)
def test_score_error_when_custom_objective_not_instantiated(problem_type, logistic_regression_binary_pipeline_class,
                                                            dummy_multiclass_pipeline_class,
                                                            dummy_regression_pipeline_class, X_y_binary):
    pipeline = dummy_regression_pipeline_class({})
    if is_binary(problem_type):
        pipeline = logistic_regression_binary_pipeline_class({})
    elif is_multiclass(problem_type):
        pipeline = dummy_multiclass_pipeline_class({})

    X, y = X_y_binary
    pipeline.fit(X, y)
    msg = "Cannot pass cost benefit matrix as a string in pipeline.score. Instantiate first and then add it to the list of objectives."
    with pytest.raises(ObjectiveCreationError, match=msg):
        pipeline.score(X, y, objectives=["cost benefit matrix", "F1"])

    # Verify ObjectiveCreationError only raised when string matches an existing objective
    with pytest.raises(ObjectiveNotFoundError, match="cost benefit is not a valid Objective!"):
        pipeline.score(X, y, objectives=["cost benefit", "F1"])

    # Verify no exception when objective properly specified
    if is_binary(problem_type):
        pipeline.score(X, y, objectives=[CostBenefitMatrix(1, 1, -1, -1), "F1"])


def test_binary_pipeline_string_target_thresholding(make_data_type, logistic_regression_binary_pipeline_class, X_y_binary):
    X, y = X_y_binary
    X = make_data_type('ww', X)
    y = make_data_type('ww', pd.Series([f"String value {i}" for i in y]))
    objective = get_objective("F1", return_instance=True)
    pipeline = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    pipeline.fit(X, y)
    assert pipeline.threshold is None
    pred_proba = pipeline.predict_proba(X, y).iloc[:, 1]
    pipeline.optimize_threshold(X, y, pred_proba, objective)
    assert pipeline.threshold is not None


@patch('evalml.pipelines.BinaryClassificationPipeline.fit')
@patch('evalml.pipelines.BinaryClassificationPipeline.score')
@patch('evalml.pipelines.BinaryClassificationPipeline.predict_proba')
@patch('evalml.pipelines.MulticlassClassificationPipeline.fit')
@patch('evalml.pipelines.MulticlassClassificationPipeline.score')
@patch('evalml.pipelines.MulticlassClassificationPipeline.predict')
def test_pipeline_thresholding_errors(mock_multi_predict, mock_multi_score, mock_multi_fit,
                                      mock_binary_pred_proba, mock_binary_score, mock_binary_fit,
                                      make_data_type, logistic_regression_binary_pipeline_class,
                                      logistic_regression_multiclass_pipeline_class, X_y_multi, X_y_binary):
    X, y = X_y_multi
    X = make_data_type('ww', X)
    y = make_data_type('ww', pd.Series([f"String value {i}" for i in y]))
    objective = get_objective("F1 Macro", return_instance=True)
    pipeline = logistic_regression_multiclass_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    pipeline.fit(X, y)
    pred_proba = pipeline.predict(X, y)
    with pytest.raises(ValueError, match="Problem type must be binary and objective must be optimizable"):
        pipeline.optimize_threshold(X, y, pred_proba, objective)

    objective = get_objective("Log Loss Multiclass")
    with pytest.raises(ValueError, match="Problem type must be binary and objective must be optimizable"):
        pipeline.optimize_threshold(X, y, pred_proba, objective)
    X, y = X_y_binary
    X = make_data_type('ww', X)
    y = make_data_type('ww', pd.Series([f"String value {i}" for i in y]))
    objective = get_objective("Log Loss Binary", return_instance=True)
    pipeline = logistic_regression_binary_pipeline_class(parameters={"Logistic Regression Classifier": {"n_jobs": 1}})
    pipeline.fit(X, y)
    pred_proba = pipeline.predict_proba(X, y).iloc[:, 1]
    with pytest.raises(ValueError, match="Problem type must be binary and objective must be optimizable"):
        pipeline.optimize_threshold(X, y, pred_proba, objective)
