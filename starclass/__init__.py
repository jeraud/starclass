#!/usr/bin/env python
# -*- coding: utf-8 -*-

from .StellarClasses import StellarClasses
from .BaseClassifier import BaseClassifier, STATUS
from .taskmanager import TaskManager
from .RFGCClassifier import RFGCClassifier
from .SLOSH import SLOSHClassifier
from .XGBClassifier import XGBClassifier
from .MetaClassifier import MetaClassifier
from .utilities import PICKLE_DEFAULT_PROTOCOL
from . import training_sets