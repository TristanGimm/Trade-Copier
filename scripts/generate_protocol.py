"""Regenerate committed classes from the official vendored cTrader schemas."""
from pathlib import Path
import re
from grpc_tools import protoc
root=Path(__file__).resolve().parents[1]/'copier'/'protocol'
files=sorted(root.glob('*.proto'))
args=['protoc','-I'+str(root),'--python_out='+str(root),'--descriptor_set_out='+str(root/'schema.desc'),'--include_imports',*[str(p) for p in files]]
if protoc.main(args):raise SystemExit('Protobuf generation failed')
for path in root.glob('*_pb2.py'):
    path.write_text(re.sub(r'^import (OpenApi\w+_pb2) as ',r'from . import \1 as ',path.read_text(),flags=re.M))
