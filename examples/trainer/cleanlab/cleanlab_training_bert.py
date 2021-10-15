import logging
import argparse
import json
import os
import statistics
import sys
from itertools import product

from torch import Tensor, LongTensor
from torch.nn import CrossEntropyLoss
from torch.optim import Adam
from torch.utils.data import TensorDataset
from transformers import DistilBertTokenizer, DistilBertForSequenceClassification, AdamW
from scipy.stats import sem

from examples.trainer.preprocessing import get_tfidf_features, convert_text_to_transformer_input
from examples.utils import read_train_dev_test
from knodle.model.logistic_regression_model import LogisticRegressionModel
from knodle.trainer.cleanlab.cleanlab import CleanLabTrainer
from knodle.trainer.cleanlab.config import CleanLabConfig


logger = logging.getLogger(__name__)


def train_cleanlab_bert(path_to_data: str, output_file: str) -> None:
    """ This is an example of launching cleanlab trainer with BERT model """

    num_experiments = 30

    parameters = dict(
        # seed=None,
        cv_n_folds=[3, 5, 8],
        p=[0.1, 0.3, 0.5, 0.7, 0.9],
        iterations=[50],
        prune_method=['prune_by_noise_rate'],               # , 'prune_by_class', 'both'
        psx_calculation_method=['signatures'],      # how the splitting into folds will be performed
        psx_epochs=[20],
        psx_lr=[0.8]
    )
    parameter_values = [v for v in parameters.values()]

    df_train, _, df_test, train_rule_matches_z, _, mapping_rules_labels_t = read_train_dev_test(path_to_data)

    # the psx matrix is calculated with logistic regression model (with TF-IDF features)
    train_input_x, test_input_x, _ = get_tfidf_features(df_train["sample"], test_data=df_test["sample"])
    X_train_tfidf = TensorDataset(Tensor(train_input_x.toarray()))

    # create test labels dataset
    test_labels = df_test["label"].tolist()
    test_labels_dataset = TensorDataset(LongTensor(test_labels))

    num_classes = max(test_labels) + 1

    # the classifier training is realized with BERT model (with BERT encoded features - input indices & attention mask)
    tokenizer = DistilBertTokenizer.from_pretrained('distilbert-base-uncased')
    X_train_bert = convert_text_to_transformer_input(df_train["sample"].tolist(), tokenizer)
    X_test_bert = convert_text_to_transformer_input(df_test["sample"].tolist(), tokenizer)

    results = []
    for run_id, (params) in enumerate(product(*parameter_values)):

        cv_n_folds, p, iterations, prune_method, psx_calculation_method, psx_epochs, psx_lr = params

        logger.info("======================================")
        logger.info(f"Parameters: seed = None cv_n_folds = {cv_n_folds} prune_method = {prune_method} prior = False "
                    f"psx_calculation_method = {psx_calculation_method} psx_epochs = {psx_epochs} psx_lr = {psx_lr} ")
        logger.info("======================================")

        exp_results_acc, exp_results_prec, exp_results_recall, exp_results_f1 = [], [], [], []

        for exp in range(0, num_experiments):

            model_logreg = LogisticRegressionModel(train_input_x.shape[1], num_classes)
            model_bert = DistilBertForSequenceClassification.from_pretrained(
                'distilbert-base-uncased', num_labels=num_classes
            )

            custom_cleanlab_config = CleanLabConfig(
                cv_n_folds=cv_n_folds,
                psx_calculation_method=psx_calculation_method,
                prune_method=prune_method,
                iterations=iterations,
                use_prior=False,
                p=p,
                output_classes=num_classes,
                optimizer=AdamW,
                criterion=CrossEntropyLoss,
                use_probabilistic_labels=False,
                lr=0.0001,
                epochs=2,
                batch_size=32,
                grad_clipping=5,

                psx_epochs=psx_epochs,
                psx_lr=psx_lr,
                psx_optimizer=Adam,
            )

            trainer = CleanLabTrainer(
                model=model_bert,
                mapping_rules_labels_t=mapping_rules_labels_t,
                model_input_x=X_train_bert,
                rule_matches_z=train_rule_matches_z,
                trainer_config=custom_cleanlab_config,

                psx_model=model_logreg,
                psx_model_input_x=X_train_tfidf
            )

            trainer.train()
            clf_report = trainer.test(X_test_bert, test_labels_dataset)
            logger.info(f"Accuracy is: {clf_report['accuracy']}")
            logger.info(f"Precision is: {clf_report['macro avg']['precision']}")
            logger.info(f"Recall is: {clf_report['macro avg']['recall']}")
            logger.info(f"F1 is: {clf_report['macro avg']['f1-score']}")
            logger.info(clf_report)

            exp_results_acc.append(clf_report['accuracy'])
            exp_results_prec.append(clf_report['macro avg']['precision'])
            exp_results_recall.append(clf_report['macro avg']['recall'])
            exp_results_f1.append(clf_report['macro avg']['f1-score'])

        result = {
            "lr": lr, "cv_n_folds": cv_n_folds, "p": p, "prune_method": prune_method, "epochs": epochs,
            "batch_size": batch_size, "psx_calculation_method": psx_calculation_method,
            "accuracy": exp_results_acc,
            "mean_accuracy": statistics.mean(exp_results_acc), "std_accuracy": statistics.stdev(exp_results_acc),
            "sem_accuracy": sem(exp_results_acc),
            "precision": exp_results_prec,
            "mean_precision": statistics.mean(exp_results_prec), "std_precision": statistics.stdev(exp_results_prec),
            "sem_precision": sem(exp_results_prec),
            "recall": exp_results_recall,
            "mean_recall": statistics.mean(exp_results_recall), "std_recall": statistics.stdev(exp_results_recall),
            "sem_recall": sem(exp_results_recall),
            "f1-score": exp_results_f1,
            "mean_f1": statistics.mean(exp_results_f1), "std_f1": statistics.stdev(exp_results_f1),
            "sem_f1": sem(exp_results_f1),
        }
        results.append(result)

        logger.info("======================================")
        logger.info(f"Params: cv_n_folds = {result['cv_n_folds']}, "
                    f"prior = {custom_cleanlab_config['use_prior']}, "
                    f"p = {result['p']}")
        logger.info(
            f"Experiments: {num_experiments} \n"
            f"Average accuracy: {result['mean_accuracy']}, std: {result['std_accuracy']}, "
            f"sem: {result['sem_accuracy']} \n"
            f"Average prec: {result['mean_precision']}, std: {result['std_precision']}, "
            f"sem: {result['sem_precision']} \n"
            f"Average recall: {result['mean_recall']}, std: {result['std_recall']}, "
            f"sem: {result['sem_recall']} \n"
            f"Average F1: {result['std_f1']}, std: {result['std_f1']}, "
            f"sem: {result['sem_f1']}")
        logger.info("======================================")

    with open(os.path.join(path_to_data, output_file), 'w') as file:
        json.dump(results, file)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]))
    parser.add_argument("--path_to_data", help="")
    parser.add_argument("--output_file", help="")

    args = parser.parse_args()
    train_cleanlab_bert(args.path_to_data, args.output_file)
