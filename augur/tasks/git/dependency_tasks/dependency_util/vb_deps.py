import sys
import re
from pathlib import Path

def get_files(path):
	#copied from example on https://docs.python.org/3/library/pathlib.html
	dir = path
	p = Path(dir)
	return list(p.glob('**/*.vb'))
	
def get_deps_for_file(path):
	with open(path, 'r') as f:
		matches = re.findall("Imports\s*(.*)", f.read())
	return matches
