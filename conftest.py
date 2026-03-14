import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# Verwijder testdatabase voor pytest import (voorkomt UNIQUE constraint bij herstart)
_test_db = "/tmp/eendjes_test.db"
for _f in [_test_db, _test_db + "-wal", _test_db + "-shm"]:
    try:
        os.unlink(_f)
    except FileNotFoundError:
        pass
