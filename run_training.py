#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Utility function for running classifiers.

.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
"""

import argparse
import logging
import starclass

#----------------------------------------------------------------------------------------------
def main():
	# Parse command line arguments:
	parser = argparse.ArgumentParser(description='Utility function for training stellar classifiers.')
	parser.add_argument('-c', '--classifier', help='Classifier to train.', default='rfgc', choices=('rfgc', 'slosh', 'foptics', 'xgb', 'sortinghat', 'meta'))
	parser.add_argument('-l', '--level', help='Classification level', default='L1', choices=('L1', 'L2'))
	#parser.add_argument('--datalevel', help="", default='corr', choices=('raw', 'corr')) # TODO: Come up with better name than "datalevel"?
	parser.add_argument('-t', '--trainingset', help='Train classifier using this training-set.', default='keplerq9', choices=('tdasim', 'keplerq9', 'keplerq9-linfit', 'keplerq9v2'))
	parser.add_argument('-tf', '--testfraction', help='Test-set fraction', type=float, default=0.0)
	parser.add_argument('-o', '--overwrite', help='Overwrite existing results.', action='store_true')
	parser.add_argument('-d', '--debug', help='Print debug messages.', action='store_true')
	parser.add_argument('-q', '--quiet', help='Only report warnings and errors.', action='store_true')
	args = parser.parse_args()

	# Check args
	if args.testfraction < 0 or args.testfraction >= 1:
		parser.error('Testfraction must be between 0 and 1')

	# Set logging level:
	logging_level = logging.INFO
	if args.quiet:
		logging_level = logging.WARNING
	elif args.debug:
		logging_level = logging.DEBUG

	# Setup logging:
	formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
	console = logging.StreamHandler()
	console.setFormatter(formatter)
	logger = logging.getLogger(__name__)
	logger.addHandler(console)
	logger.setLevel(logging_level)
	logger_parent = logging.getLogger('starclass')
	logger_parent.addHandler(console)
	logger_parent.setLevel(logging_level)

	# Choose which classifier to use
	current_classifier = args.classifier
	ClassificationClass = {
		'rfgc': starclass.RFGCClassifier,
		'slosh': starclass.SLOSHClassifier,
		#'foptics': starclass.FOPTICSClassifier,
		'xgb': starclass.XGBClassifier,
		'sortinghat': starclass.SortingHatClassifier,
		'meta': starclass.MetaClassifier
	}

	# Settings to be passed onto the selected training set:
	tset_settings = {
		'datalevel': 'corr',
		'tf': args.testfraction,
		'random_seed': 2359230457,
	}

	# Pick the training set:
	if args.trainingset == 'tdasim':
		tset = starclass.training_sets.tda_simulations(**tset_settings)
	elif args.trainingset == 'keplerq9':
		tset = starclass.training_sets.keplerq9(**tset_settings)
	elif args.trainingset == 'keplerq9-linfit':
		tset = starclass.training_sets.keplerq9linfit(**tset_settings)
	elif args.trainingset == 'keplerq9v2':
		tset = starclass.training_sets.keplerq9v2(**tset_settings)

	# The Meta-classifier requires us to first train all of the other classifiers
	# using cross-validation
	if current_classifier == 'meta':
		# Loop through all the other classifiers and initialize them:
		# TODO: Run in parallel?
		with starclass.TaskManager(tset.input_folder, overwrite=False) as tm:
			for cla_key in tm.all_classifiers:
				# Split the tset object into cross-validation folds.
				# These are objects with exactly the same properties as the original one,
				# except that they will run through different subsets of the training and test sets:
				cla = ClassificationClass[cla_key]
				for tset_fold in tset.folds(tf=0.2):
					tset_key = tset.key + '/meta_fold{0:02d}'.format(tset_fold.fold)
					with cla(level=args.level, features_cache=tset.features_cache, tset_key=tset_key) as stcl:
						logger.info('Training %s on Fold %d/%d...', stcl.classifier_key, tset_fold.fold, tset_fold.crossval_folds)
						stcl.train(tset_fold)
						logger.info("Classifying test-set...")
						stcl.test(tset_fold, save=True, save_func=tm.save_results)

	# Initialize the classifier:
	classifier = ClassificationClass[current_classifier]
	with starclass.TaskManager(tset.input_folder, overwrite=False) as tm:
		with classifier(level=args.level, features_cache=tset.features_cache, tset_key=tset.key) as stcl:
			# Run the training of the classifier:
			logger.info("Training %s...", current_classifier)
			stcl.train(tset)
			logger.info("Training done...")
			logger.info("Classifying test-set using %s...", current_classifier)
			stcl.test(tset, save=True, save_func=tm.save_results)

#----------------------------------------------------------------------------------------------
if __name__ == '__main__':
	main()
