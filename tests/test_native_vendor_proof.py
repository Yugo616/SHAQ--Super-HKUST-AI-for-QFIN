import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch


def fixture_pe(resource=b'public icon'):
    data=bytearray(1536)
    data[:2]=b'MZ'
    struct.pack_into('<I',data,0x3c,64)
    data[64:68]=b'PE\0\0'
    struct.pack_into('<HH',data,68,332,2)
    struct.pack_into('<H',data,84,224)
    struct.pack_into('<H',data,88,0x10b)
    struct.pack_into('<I',data,104,4096)
    for position,name,offset,flags in ((312,b'.text',512,0x60000020),(352,b'.rsrc',1024,0x40000040)):
        data[position:position+len(name)]=name
        struct.pack_into('<II',data,position+16,512,offset)
        struct.pack_into('<I',data,position+36,flags)
    code=b'C:\\Users\\runneradmin\\.rustup\\toolchains\\nightly-x86_64-pc-windows-msvc\\lib\\rustlib\\src\\rust\\library\\core\\src\\fmt.rs\0'
    data[512:512+len(code)]=code
    data[1024:1024+len(resource)]=resource
    pins={'exact_sha256':[], 'launcher':{'machine':332,'entrypoint':4096,
          'sections':{'.text':hashlib.sha256(data[512:1024]).hexdigest()}}}
    return bytes(data),pins


class NativeVendorProofTests(unittest.TestCase):
    def module(self):
        path=Path(__file__).parents[1]/'packaging/audit_payload.py'
        spec=importlib.util.spec_from_file_location('native_audit',path)
        result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
        return result

    def test_resource_customization_requires_unchanged_upstream_code_sections(self):
        audit=self.module();proof=getattr(audit,'native_vendor_identity',None)
        self.assertIsNotNone(proof,'Upstream native source paths require pinned binary evidence')
        data,pins=fixture_pe()
        self.assertTrue(proof(data,pins))
        changed=bytearray(data);changed[600]^=1
        self.assertFalse(proof(bytes(changed),pins))
        changed=bytearray(data);struct.pack_into('<I',changed,104,8192)
        self.assertFalse(proof(bytes(changed),pins))
        changed=bytearray(data);struct.pack_into('<I',changed,388,0x60000040)
        self.assertFalse(proof(bytes(changed),pins),'Executable resource sections cannot be exempt')
        changed=bytearray(data);changed[352:360]=b'.other\0\0'
        self.assertFalse(proof(bytes(changed),pins),'Unknown non-resource sections cannot be exempt')
        self.assertFalse(proof(data[:700],pins),'Truncated PE cannot prove vendor identity')

    def test_proven_vendor_rust_paths_do_not_exempt_private_resource_data(self):
        audit=self.module();proof=getattr(audit,'native_vendor_identity',None)
        self.assertIsNotNone(proof)
        home='/'.join(('C:', 'Users', 'runneradmin'));checkout='D:/a/current/current'
        for resource,want_private in ((b'public icon',False),
            ((home+'/.rustup/toolchains/nightly-x86_64-pc-windows-msvc/lib/rustlib/src/rust/library/std/src/../../backtrace/src/dbghelp.rs\0').encode(),False),
            ((home+'/.ssh/id_rsa').encode(),True),((checkout+'/src/main.py').encode(),True),
            ((home+'/.rustup/toolchains/nightly-x86_64-pc-windows-msvc/lib/rustlib/src/rust/library/../../secret').encode(),True)):
            data,pins=fixture_pe(resource)
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);target=root/'launcher.exe';target.write_bytes(data)
                with patch.object(audit,'native_vendor_pins',return_value=pins), \
                     patch.object(audit.Path,'home',return_value=Path(home)), \
                     patch.object(audit.sys,'platform','test'), \
                     patch.dict(audit.os.environ,{'GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted'}):
                    # Current checkout scanning also remains independent of proof.
                    observed=audit.contains_private_path(data,home,checkout,True,
                                                        verified_native=proof(data,pins))
                    self.assertEqual(observed,want_private)
                    if 'current' not in resource.decode():
                        private=[x for x in audit.audit(root)['failures'] if x.startswith('private build/user path:')]
                        self.assertEqual(bool(private),want_private)
                self.assertTrue(audit.contains_private_path(data,home,checkout,True),
                                'Unproven native bytes must still be rejected')

    def test_exact_updater_digest_rejects_any_mutation(self):
        proof=getattr(self.module(),'native_vendor_identity',None)
        self.assertIsNotNone(proof)
        data=b'pinned full updater';pins={'exact_sha256':[hashlib.sha256(data).hexdigest()]}
        self.assertTrue(proof(data,pins))
        self.assertFalse(proof(data+b'changed',pins))
