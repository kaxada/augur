import sys
import re
from pathlib import Path

def get_files(path):
	#copied from example on https://docs.python.org/3/library/pathlib.html
	dir = path
	p = Path(dir)
	return list(p.glob('**/*.rb'))
	
def get_deps_for_file(path):
	with open(path, 'r') as f:
		matches = re.findall('require\s*"(.*)"', f.read())
		f.seek(0)
		matches = re.findall('require_relative\s*"(.*)"', f.read())
	return matches
