# Temporary openCEM model instance for ReferenceModel.py
from cemo.core import create_model
model = create_model('openCEM',
                     unslim=True,
                     emitlimit=True,
                     nemret=False,
                     regionret=False)