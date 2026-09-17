import json
import tempfile
import unittest
from pathlib import Path

from shaq_daily_oracle.hashing import sha256_payload, sha256_file


class ModelIdentityLinkTests(unittest.TestCase):
    def test_only_explicit_hash_bound_local_link_can_join_identities(self):
        from shaq_daily_oracle.model_identity_links import read_model_links
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(read_model_links(root), {})
            anchors=[]
            for n, name in [('a', 'subscription-default'), ('b', 'example-model')]:
                path=root/'batches'/n/'variants/v/variant_result.json'
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'model_profile_sha256':n*64,'model_name':name}))
                anchors.append(dict(path=path.relative_to(root).as_posix(),sha256=sha256_file(path)))
            receipt=dict(source='user_confirmation',model='example-model',canonical_identity='a'*64,
                         identities=['a'*64,'b'*64],anchors=anchors)
            links=root/'model_identity_links';links.mkdir()
            saved=links/(sha256_payload(receipt)+'.json');saved.write_text(json.dumps(receipt))
            values=read_model_links(root)
            self.assertEqual(values['b'*64]['canonical_identity'],'a'*64)
            self.assertEqual(values['a'*64]['model'],'example-model')
            saved.write_text(json.dumps({**receipt,'model':'other-model'}))
            with self.assertRaises(ValueError):read_model_links(root)
            saved.write_text(json.dumps(receipt))
            (root/anchors[0]['path']).write_text('{}')
            with self.assertRaises(ValueError):read_model_links(root)
