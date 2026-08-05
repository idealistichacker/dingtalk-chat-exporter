import os
import tempfile
import unittest

import attachment


class AttachmentOptimizationTests(unittest.TestCase):
    def test_duplicate_source_is_copied_once_and_reused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "cached.jpg")
            with open(source, "wb") as stream:
                stream.write(b"image-data")

            export_dir = os.path.join(temp_dir, "export")
            messages = [
                {
                    "id": 1,
                    "cid": "conversation",
                    "content_type": 2,
                    "image_info": {"images": [{"local_path": source}]},
                },
                {
                    "id": 2,
                    "cid": "conversation",
                    "content_type": 2,
                    "image_info": {"images": [{"local_path": source}]},
                },
            ]

            stats = attachment.process_all_attachments(messages, export_dir)

            first = messages[0]["image_export_paths"][0]
            second = messages[1]["image_export_paths"][0]
            self.assertEqual(first, second)
            self.assertEqual(stats["deduplicated"], 1)
            image_files = os.listdir(os.path.join(export_dir, "images"))
            self.assertEqual(image_files, [os.path.basename(first)])


if __name__ == "__main__":
    unittest.main()
