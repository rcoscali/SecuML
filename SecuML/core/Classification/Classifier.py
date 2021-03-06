# SecuML
# Copyright (C) 2016-2018  ANSSI
#
# SecuML is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# SecuML is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with SecuML. If not, see <http://www.gnu.org/licenses/>.

import abc
import time

from sklearn.externals import joblib
from sklearn.model_selection import StratifiedKFold
from sklearn.model_selection import GridSearchCV

from .Configuration.TestConfiguration.CvConf import CvConf
from .CvClassifierDatasets import CvClassifierDatasets
from .Monitoring.AlertsMonitoring import AlertsMonitoring
from .Monitoring.CvMonitoring import CvMonitoring
from .Monitoring.TestingMonitoring import TestingMonitoring
from .Monitoring.TrainingMonitoring import TrainingMonitoring
from .Predictions import Predictions

from SecuML.core.Tools.core_exceptions import SecuMLcoreException


class SupervisedLearningAtLeastTwoClasses(SecuMLcoreException):

    def __str__(self):
        return('Supervised learning models requires that the training dataset '
               'contains at least two classes.')


class Classifier(object):

    def __init__(self, conf, cv_monitoring=False):
        self.conf = conf
        self.cv_monitoring = cv_monitoring
        self.createPipeline()

    @abc.abstractmethod
    def createPipeline(self):
        return

    def trainTestValidation(self, datasets):
        self.training(datasets)
        self.testValidation(datasets)

    def testValidation(self, datasets):
        self.testing(datasets)
        self.generateAlerts(datasets)
        if datasets.validation_instances is not None:
            self.validation(datasets)

    def getSupervision(self, instances, ground_truth=False, check=True):
        annotations = instances.getAnnotations(ground_truth)
        supervision = annotations.getSupervision(
            self.conf.families_supervision)
        if check:
            if len(set(supervision)) < 2:
                raise SupervisedLearningAtLeastTwoClasses
        return supervision

    def loadModel(self, model_filename):
        self.pipeline = joblib.load(model_filename)

    def dumpModel(self, model_filename):
        joblib.dump(self.pipeline, model_filename)

    def training(self, datasets):
        self.training_execution_time = 0
        start = time.time()
        self.cv = self.setBestParameters(datasets)
        self.training_execution_time += time.time() - start
        start = time.time()
        if datasets.sample_weight:
            self.pipeline.fit(datasets.train_instances.features.getValues(),
                              self.getSupervision(datasets.train_instances),
                              **{'model__sample_weight': datasets.sample_weight})
        else:
            self.pipeline.fit(datasets.train_instances.features.getValues(),
                              self.getSupervision(datasets.train_instances))
        self.training_execution_time += time.time() - start
        # Training monitoring
        self.training_predictions = self.applyPipeline(
            datasets.train_instances)
        self.training_predictions.setGroundTruth(
            self.getSupervision(datasets.train_instances,
                                ground_truth=False))
        self.setCoefficients(datasets)
        if self.conf.families_supervision:
            self.class_labels = self.pipeline.named_steps['model'].classes_
        self.training_monitoring = TrainingMonitoring(self.conf)
        self.training_monitoring.initMonitorings(datasets)
        self.training_monitoring.addFold(0,
                                         self.training_predictions,
                                         self.coefs)

    def setCoefficients(self, datasets):
        if self.conf.featureImportance() == 'score':
            self.coefs = self.pipeline.named_steps['model'].feature_importances_
        elif self.conf.featureImportance() == 'weight':
            self.coefs = self.pipeline.named_steps['model'].coef_[0]
        else:
            self.coefs = [0] * len(datasets.getFeaturesNames())

    def crossValidationMonitoring(self, datasets):
        # CV datasets
        cv_test_conf = CvConf(self.conf.num_folds, alerts_conf=None,
                              logger=self.conf.logger)
        cv_datasets = CvClassifierDatasets(cv_test_conf,
                                           self.conf.families_supervision,
                                           self.conf.sample_weight,
                                           cv=self.cv)
        cv_datasets.generateDatasets(datasets.train_instances, None)
        # CV monitoring
        self.cv_monitoring = CvMonitoring(self.conf, self.conf.num_folds)
        self.cv_monitoring.initMonitorings(cv_datasets)
        for fold_id, datasets in enumerate(cv_datasets.datasets):
            if datasets.sample_weight:
                self.pipeline.fit(
                             datasets.train_instances.features.getValues(),
                             self.getSupervision(
                                 datasets.train_instances),
                             **{'model__sample_weight': datasets.sample_weight})
            else:
                self.pipeline.fit(datasets.train_instances.features.getValues(),
                                  self.getSupervision(datasets.train_instances))
            cv_predictions = self.applyPipeline(datasets.test_instances)
            cv_predictions.setGroundTruth(
                self.getSupervision(datasets.test_instances,
                                    ground_truth=False))
            try:
                coefs = self.pipeline.named_steps['model'].coef_[0]
            except:
                coefs = [0] * len(datasets.getFeaturesNames())
            self.cv_monitoring.addFold(fold_id, cv_predictions, coefs)

    def applyPipeline(self, instances):
        num_instances = instances.numInstances()
        if num_instances == 0:
            return Predictions([], [], [], [], instances.ids)
        features = instances.features.getValues()
        predictions = self.pipeline.predict(features)
        all_predicted_proba = self.pipeline.predict_proba(features)
        if self.conf.families_supervision:
            predicted_proba = [None for i in range(num_instances)]
        else:
            predicted_proba = all_predicted_proba[:, 1]
        try:
            predicted_scores = self.pipeline.decision_function(features)
        except:
            predicted_scores = [0 for i in range(num_instances)]
        predictions_obj = Predictions(list(predictions), all_predicted_proba,
                                      predicted_proba, predicted_scores,
                                      instances.ids)
        return predictions_obj

    def setBestParameters(self, datasets):
        cv = StratifiedKFold(n_splits=self.conf.num_folds)
        param_grid = self.conf.getParamGrid()
        if param_grid is None:
            # No parameter value to select
            return cv
        if self.conf.families_supervision:
            scoring = 'accuracy'
        else:
            scoring = 'roc_auc'
        grid_search = GridSearchCV(self.pipeline,
                                   param_grid=param_grid,
                                   scoring=scoring,
                                   cv=cv,
                                   n_jobs=self.conf.n_jobs)
        if datasets.sample_weight:
            grid_search.fit(datasets.train_instances.features.getValues(),
                            self.getSupervision(datasets.train_instances)
                            ** {'model__sample_weight': datasets.sample_weight})
        else:
            grid_search.fit(datasets.train_instances.features.getValues(),
                            self.getSupervision(datasets.train_instances))
        self.conf.setBestValues(grid_search)
        self.pipeline.set_params(**self.conf.getBestValues())
        return cv

    def testing(self, datasets):
        # Testing
        start = time.time()
        self.testing_predictions = self.applyPipeline(datasets.test_instances)
        self.testing_predictions.setGroundTruth(
            self.getSupervision(datasets.test_instances,
                                ground_truth=True,
                                check=False))
        self.testing_execution_time = time.time() - start
        # Monitoring
        self.testing_monitoring = TestingMonitoring(self.conf)
        self.testing_monitoring.initMonitorings(datasets)
        self.testing_monitoring.addPredictions(self.testing_predictions)

    def validation(self, datasets):
        # Validation
        self.validation_predictions = self.applyPipeline(
            datasets.validation_instances)
        self.validation_predictions.setGroundTruth(
            self.getSupervision(datasets.validation_instances,
                                ground_truth=True,
                                check=False))
        # Monitoring
        self.validation_monitoring = TestingMonitoring(self.conf,
                                                       monitoring_type='validation')
        self.validation_monitoring.initMonitorings(datasets)
        self.validation_monitoring.addPredictions(self.validation_predictions)

    def generateAlerts(self, datasets):
        self.alerts = None
        if self.conf.families_supervision:
            return
        if self.conf.test_conf.alerts_conf is None:
            return
        predictions = self.testing_monitoring.predictions_monitoring
        alerts_conf = self.conf.test_conf.alerts_conf
        self.alerts = AlertsMonitoring(datasets, predictions, alerts_conf)
        self.alerts.groupAlerts()
