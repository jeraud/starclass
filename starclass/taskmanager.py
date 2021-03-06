#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
A TaskManager which keeps track of which targets to process.

.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
"""

import numpy as np
import os
import sqlite3
import logging
from astropy.table import Table
from . import STATUS
from .constants import classifier_list
from .version import get_version

#--------------------------------------------------------------------------------------------------
class TaskManager(object):
	"""
	A TaskManager which keeps track of which targets to process.
	"""

	def __init__(self, todo_file, cleanup=False, readonly=False, overwrite=False, classes=None):
		"""
		Initialize the TaskManager which keeps track of which targets to process.

		Parameters:
			todo_file (str): Path to the TODO-file.
			cleanup (bool): Perform cleanup/optimization of TODO-file before
				doing initialization. Default=False.
			overwrite (bool): Overwrite any previously calculated results. Default=False.
			classes (Enum): Possible stellar classes. This is only used for for translating
				saved stellar classes in the ``other_classifiers`` table into proper enums.

		Raises:
			FileNotFoundError: If TODO-file could not be found.
		"""

		if os.path.isdir(todo_file):
			todo_file = os.path.join(todo_file, 'todo.sqlite')

		if not os.path.exists(todo_file):
			raise FileNotFoundError('Could not find TODO-file')

		self.StellarClasses = classes
		self.readonly = readonly
		self.tset = None
		self.input_folder = os.path.abspath(os.path.dirname(todo_file))
		self._moat_tables = {}

		# Keep a list of all the possible classifiers here:
		self.all_classifiers = list(classifier_list)
		self.all_classifiers.remove('meta')
		self.all_classifiers = set(self.all_classifiers)

		# Setup logging:
		formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
		console = logging.StreamHandler()
		console.setFormatter(formatter)
		self.logger = logging.getLogger(__name__)
		self.logger.addHandler(console)
		self.logger.setLevel(logging.INFO)

		# Load the SQLite file:
		#if self.readonly:
		#	self.conn = sqlite3.connect('file:' + todo_file + '?mode=ro', uri=True)
		#else:
		self.conn = sqlite3.connect(todo_file)
		self.conn.row_factory = sqlite3.Row
		self.cursor = self.conn.cursor()
		self.cursor.execute("PRAGMA foreign_keys=ON;")
		self.cursor.execute("PRAGMA locking_mode=EXCLUSIVE;")
		self.cursor.execute("PRAGMA journal_mode=TRUNCATE;")

		# Find out if corrections have been run:
		self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='diagnostics_corr';")
		if self.cursor.fetchone() is None:
			raise ValueError("The TODO-file does not contain diagnostics_corr. Are you sure corrections have been run?")

		# Find existing MOAT tables in the todo-file:
		self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'starclass_features_%';")
		for row in self.cursor.fetchall():
			classifier = row['name'].replace('starclass_features_', '')
			self.cursor.execute("PRAGMA table_info(" + row['name'] + ");")
			columns = [col['name'] for col in self.cursor.fetchall()]
			columns.remove('priority')
			self.moat_create(classifier, columns)

		# Reset the status of everything for a new run:
		if overwrite:
			self.cursor.execute("DROP TABLE IF EXISTS starclass_settings;")
			self.cursor.execute("DROP TABLE IF EXISTS starclass_diagnostics;")
			self.cursor.execute("DROP TABLE IF EXISTS starclass_results;")
			self.conn.commit()
			cleanup = True # Enforce a cleanup after deleting old results

		# Create table for settings if it doesn't already exits:
		self.cursor.execute("""CREATE TABLE IF NOT EXISTS starclass_settings (
			tset TEXT NOT NULL,
			version TEXT NOT NULL
		);""")
		self.conn.commit()

		# Load settings from setting tables:
		self.cursor.execute("SELECT * FROM starclass_settings LIMIT 1;")
		row = self.cursor.fetchone()
		if row is not None:
			self.tset = row['tset']

		# Create table for diagnostics:
		self.cursor.execute("""CREATE TABLE IF NOT EXISTS starclass_diagnostics (
			priority INTEGER NOT NULL,
			classifier TEXT NOT NULL,
			status INTEGER NOT NULL,
			elaptime REAL,
			worker_wait_time REAL,
			errors TEXT,
			PRIMARY KEY (priority, classifier),
			FOREIGN KEY (priority) REFERENCES todolist(priority) ON DELETE CASCADE ON UPDATE CASCADE
		);""")
		self.cursor.execute("CREATE INDEX IF NOT EXISTS starclass_diag_status_idx ON starclass_diagnostics (status);")
		self.cursor.execute("""CREATE TABLE IF NOT EXISTS starclass_results (
			priority INTEGER NOT NULL,
			classifier TEXT NOT NULL,
			class TEXT NOT NULL,
			prob REAL NOT NULL,
			FOREIGN KEY (priority, classifier) REFERENCES starclass_diagnostics(priority, classifier) ON DELETE CASCADE ON UPDATE CASCADE
		);""")
		self.cursor.execute("CREATE INDEX IF NOT EXISTS starclass_resu_priority_classifier_idx ON starclass_results (priority, classifier);")

		# Make sure we have proper indicies that should have been created by the previous pipeline steps:
		self.cursor.execute("CREATE INDEX IF NOT EXISTS corr_status_idx ON todolist (corr_status);")

		# Find out if data-validation information exists:
		self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='datavalidation_corr';")
		self.datavalidation_exists = (self.cursor.fetchone() is not None)
		if not self.datavalidation_exists:
			self.logger.warning("DATA-VALIDATION information is not available in this TODO-file. Assuming all targets are good.")

		# Analyze the tables for better query planning:
		self.cursor.execute("ANALYZE;")
		self.conn.commit()

		# Run a cleanup/optimization of the database before we get started:
		if cleanup:
			self.logger.debug("Cleaning TODOLIST before run...")
			try:
				self.conn.isolation_level = None
				self.cursor.execute("VACUUM;")
			finally:
				self.conn.isolation_level = ''

	#----------------------------------------------------------------------------------------------
	def close(self):
		"""Close TaskManager and all associated objects."""
		if hasattr(self, 'cursor') and hasattr(self, 'conn') and self.conn:
			try:
				self.conn.rollback()
				self.cursor.execute("PRAGMA journal_mode=DELETE;")
				self.conn.commit()
				self.cursor.close()
			except sqlite3.ProgrammingError: # pragma: no cover
				pass

		if hasattr(self, 'conn') and self.conn:
			self.conn.close()
			self.conn = None

	#----------------------------------------------------------------------------------------------
	def __del__(self):
		self.close()

	#----------------------------------------------------------------------------------------------
	def __exit__(self, *args):
		self.close()

	#----------------------------------------------------------------------------------------------
	def __enter__(self):
		return self

	#----------------------------------------------------------------------------------------------
	def get_number_tasks(self):
		"""
		Get number of tasks due to be processed.

		Returns:
			int: Number of tasks due to be processed.
		"""
		raise NotImplementedError()

	#----------------------------------------------------------------------------------------------
	def _query_task(self, classifier=None, priority=None):

		search_joins = []
		search_query = []

		# TODO: Is this right?
		if classifier is None and priority is None:
			raise ValueError("This will just give the same again and again")

		# Build list of constraints:
		if priority is not None:
			search_query.append('todolist.priority=%d' % priority)

		# If data-validation information is available, only include targets
		# which passed the data validation:
		if self.datavalidation_exists:
			search_joins.append("INNER JOIN datavalidation_corr ON datavalidation_corr.priority=todolist.priority")
			search_query.append("datavalidation_corr.approved=1")

		# If a classifier is specified, constrain to only that classifier:
		if classifier is not None:
			search_joins.append("LEFT JOIN starclass_diagnostics ON starclass_diagnostics.priority=todolist.priority AND starclass_diagnostics.classifier='{classifier:s}'".format(
				classifier=classifier
			))
			search_query.append("starclass_diagnostics.status IS NULL")

		# Build query string:
		# Note: It is not possible for search_query to be empty!
		search_joins = "\n".join(search_joins)
		search_query = "AND " + " AND ".join(search_query)

		self.cursor.execute("""
			SELECT
				todolist.priority,
				todolist.starid,
				todolist.tmag,
				diagnostics_corr.lightcurve AS lightcurve,
				diagnostics_corr.variance,
				diagnostics_corr.rms_hour,
				diagnostics_corr.ptp
			FROM
				todolist
				INNER JOIN diagnostics_corr ON todolist.priority=diagnostics_corr.priority
				{joins:s}
			WHERE
				todolist.corr_status IN ({ok:d},{warning:d})
				{constraints:s}
			ORDER BY todolist.priority LIMIT 1;""".format(
			ok=STATUS.OK.value,
			warning=STATUS.WARNING.value,
			joins=search_joins,
			constraints=search_query
		))
		task = self.cursor.fetchone()
		if task:
			task = dict(task)
			task['classifier'] = classifier
			task['lightcurve'] = os.path.join(self.input_folder, task['lightcurve'])

			# Add things from the catalog file:
			#catalog_file = os.path.join(????, 'catalog_sector{sector:03d}_camera{camera:d}_ccd{ccd:d}.sqlite')
			# cursor.execute("SELECT ra,decl as dec,teff FROM catalog WHERE starid=?;", (task['starid'], ))
			#task.update()

			# Add common features already calculated by some other classifier:
			# This is not needed for the meta-classifier
			if classifier != 'meta':
				features_common = self.moat_query('common', task['priority'])
				if features_common is not None:
					task['features_common'] = features_common
				if classifier is not None:
					features_specific = self.moat_query(classifier, task['priority'])
					if features_specific is not None:
						task['features'] = features_specific

			# If the classifier that is running is the meta-classifier,
			# add the results from all other classifiers to the task dict:
			# FIXME: Enforce this for META only. The problem is the TrainingSet class, which doesn't know about which classifier is running it
			if classifier == 'meta' or classifier is None:
				self.cursor.execute("SELECT starclass_results.classifier,class,prob FROM starclass_results INNER JOIN starclass_diagnostics ON starclass_results.priority=starclass_diagnostics.priority AND starclass_results.classifier=starclass_diagnostics.classifier WHERE starclass_results.priority=? AND status=? AND starclass_results.classifier != 'meta' ORDER BY starclass_results.classifier, class;", [
					task['priority'],
					STATUS.OK.value
				])

				# Add as a Table to the task list:
				rows = []
				for r in self.cursor.fetchall():
					rows.append([r['classifier'], self.StellarClasses[r['class']], r['prob']])
				if not rows: rows = None
				task['other_classifiers'] = Table(
					rows=rows,
					names=('classifier', 'class', 'prob'),
				)
			else:
				task['other_classifiers'] = None

			return task
		return None

	#----------------------------------------------------------------------------------------------
	def get_task(self, priority=None, classifier=None, change_classifier=True):
		"""
		Get next task to be processed.

		Parameters:
			priority (integer):
			classifier (string): Classifier to get next task for.
				If no tasks are available for this classifier, and `change_classifier=True`,
				a task for another classifier will be returned.
			change_classifier (boolean): Return task for another classifier
				if there are no more tasks for the provided classifier.
				Default=True.

		Returns:
			dict or None: Dictionary of settings for task.
		"""

		task = None
		task = self._query_task(classifier=classifier, priority=priority)

		# If no task is returned for the given classifier, find another
		# classifier where tasks are available:
		if task is None and change_classifier:
			# Make a search on all the classifiers, and record the next
			# task for all of them:
			all_tasks = []
			for cl in self.all_classifiers.difference([classifier]):
				task = self._query_task(classifier=cl, priority=priority)
				if task is not None:
					all_tasks.append(task)

			# Pick the classifier that has reached the lowest priority:
			if all_tasks:
				indx = np.argmin([t['priority'] for t in all_tasks])
				return all_tasks[indx]

			# If this is reached, all classifiers are done, and we can
			# start running the MetaClassifier:
			task = self._query_task(classifier='meta', priority=priority)

		return task

	#----------------------------------------------------------------------------------------------
	def save_settings(self):
		"""
		Save settings to TODO-file and create method-specific columns in ``diagnostics_corr`` table.

		.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
		"""
		try:
			self.cursor.execute("DELETE FROM starclass_settings;")
			self.cursor.execute("INSERT INTO starclass_settings (tset,version) VALUES (?,?);", [
				self.tset,
				get_version()
			])
			self.conn.commit()
		except: # noqa: E722, pragma: no cover
			self.conn.rollback()
			raise

	#----------------------------------------------------------------------------------------------
	def moat_create(self, classifier, columns):
		# Just some checks of the input:
		if classifier != 'common' and classifier not in self.all_classifiers:
			raise ValueError("Invalid classifier: %s" % classifier)
		if not columns:
			raise ValueError("Invalid column names provided")

		#db_name = 'db_' + classifier
		table_name = "starclass_features_" + classifier

		columns = sorted(columns)
		columns_insert = ",".join(columns)
		columns_create = ",\n".join(['"' + key + '" REAL' for key in columns])
		placeholders = ",".join([':' + key for key in columns])

		# Create table:
		#print("ATTACH DATABASE '' AS {db_name:s};".format(
		#	db_name=db_name
		#))
		query_create = """
		CREATE TABLE IF NOT EXISTS {table_name:s} (
			priority INTEGER NOT NULL PRIMARY KEY,
			{columns:s},
			FOREIGN KEY (priority) REFERENCES diagnostics_corr(priority) ON DELETE CASCADE ON UPDATE CASCADE
		);""".format(
			table_name=table_name,
			columns=columns_create
		)
		self.cursor.execute(query_create)
		self.cursor.execute("ANALYZE;")

		# Generate SQL statement which will be used to insert extracted features
		# into this table:
		query_insert = "INSERT OR REPLACE INTO {table_name:s} (priority,{columns:s}) VALUES (:priority,{placeholders:s});".format(
			table_name=table_name,
			columns=columns_insert,
			placeholders=placeholders
		)

		# Generate SQL statement which will be used to select extracted features
		# from this table:
		query_select = "SELECT {columns:s} FROM {table_name:s} WHERE priority=?;".format(
			table_name=table_name,
			columns=columns_insert
		)

		# Gather into dict and save to memory for later reuse:
		query = {
			'table_name': table_name,
			'insert': query_insert,
			'select': query_select,
		}
		self._moat_tables[classifier] = query
		return query

	#----------------------------------------------------------------------------------------------
	def _moat_insert(self, classifier, priority, features):
		"""
		Insert extracted features into Mother Of All Tables (MOAT).

		Parameters:
			classifier (str):
			priority (int):
			features (dict):

		.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
		"""

		query = self._moat_tables.get(classifier)
		if query is None:
			query = self.moat_create(classifier, features.keys())

		# Insert into MOAT table using pre-compiled SQL query:
		priority_dict = {'priority': priority}
		self.cursor.execute(query['insert'], {**features, **priority_dict})

	#----------------------------------------------------------------------------------------------
	def moat_query(self, classifier, priority):
		"""
		Query Mother Of All Tables (MOAT) for cached features.

		Parameters:
			classifier (str):
			priority (int):

		Returns:
			dict: Dictionary with features stores in MOAT.

		.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
		"""
		query = self._moat_tables.get(classifier)
		if query is not None:
			self.cursor.execute(query['select'], [priority])
			row = self.cursor.fetchone()
			if row:
				return {key: (np.NaN if val is None else val) for key, val in dict(row).items()}
		return None

	#----------------------------------------------------------------------------------------------
	def moat_clear(self):
		"""
		Clear Mother Of All Tables (MOAT).

		.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
		"""
		for query in self._moat_tables.values():
			self.cursor.execute("DROP TABLE {table_name:s};".format(
				table_name=query['table_name']
			))
		self.conn.commit()
		self._moat_tables.clear()

		# Run a VACUUM of todo-file after potentially deleting many tables:
		self.logger.debug("Cleaning TODOLIST after moat_clear...")
		try:
			self.conn.isolation_level = None
			self.cursor.execute("VACUUM;")
		finally:
			self.conn.isolation_level = ''

	#----------------------------------------------------------------------------------------------
	def save_results(self, result):
		"""
		Save results and diagnostics. This will update the TODO list.

		Parameters:
			results (dict): Dictionary of results and diagnostics.

		Raises:
			ValueError: If attempting to save results from multiple different training sets.

		.. codeauthor:: Rasmus Handberg <rasmush@phys.au.dk>
		"""

		# If the training set has not already been set for this TODO-file,
		# update the settings, and if it has check that we are not
		# mixing results from different correctors in one TODO-file.
		tset = result.get('tset')
		if self.tset is None and tset:
			self.tset = tset
			self.save_settings()
		elif tset != self.tset:
			raise ValueError("Attempting to mix results from multiple training sets. Previous='%s', New='%s'." % (self.tset, tset))

		priority = result.get('priority')
		classifier = result.get('classifier')
		status = result.get('status')
		details = result.get('details', {})
		starclass_results = result.get('starclass_results', {})
		common = result.get('features_common')
		features = result.get('features')

		# Save additional diagnostics:
		error_msg = details.get('errors', None)
		if error_msg:
			error_msg = '\n'.join(error_msg)
			#self.summary['last_error'] = error_msg

		# Store the results in database:
		try:
			# Save additional diagnostics:
			self.cursor.execute("INSERT OR REPLACE INTO starclass_diagnostics (priority,classifier,status,errors,elaptime,worker_wait_time) VALUES (:priority,:classifier,:status,:errors,:elaptime,:worker_wait_time);", {
				'priority': priority,
				'classifier': classifier,
				'status': status.value,
				'elaptime': result.get('elaptime'),
				'worker_wait_time': result.get('worker_wait_time'),
				'errors': error_msg
			})

			self.cursor.execute("DELETE FROM starclass_results WHERE priority=? AND classifier=?;", (priority, classifier))
			for key, value in starclass_results.items():
				self.cursor.execute("INSERT INTO starclass_results (priority,classifier,class,prob) VALUES (:priority,:classifier,:class,:prob);", {
					'priority': priority,
					'classifier': classifier,
					'class': key.name,
					'prob': value
				})

			# Save common features if they are provided:
			if common:
				self._moat_insert('common', priority, common)

			# Save classifier-specific features if they are provided:
			if features:
				self._moat_insert(classifier, priority, features)

			self.conn.commit()
		except: # noqa: E722, pragma: no cover
			self.conn.rollback()
			raise

	#----------------------------------------------------------------------------------------------
	def start_task(self, task):
		"""
		Mark a task as STARTED in the TODO-list.
		"""
		try:
			self.cursor.execute("INSERT INTO starclass_diagnostics (priority,classifier,status) VALUES (:priority,:classifier,:status);", {
				'priority': task['priority'],
				'classifier': task['classifier'],
				'status': STATUS.STARTED.value
			})
			self.conn.commit()
		except: # noqa: E722, pragma: no cover
			self.conn.rollback()
			raise
