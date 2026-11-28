"""Single-frame and signed-pair checks without a camera or a model process."""

import io
import unittest
from unittest.mock import patch

from mind import model_pack, vision_describer


class VisionDescriber(unittest.TestCase):
    def test_input_must_be_one_exact_bounded_colour_frame(self):
        frame = b"P6\n1 1\n255\n\x10\x20\x30"
        self.assertEqual(vision_describer.read_rgb(io.BytesIO(frame)), frame)
        for invalid in (frame[:-1], frame + b"second", b"P6\n321 1\n255\n", b"P5\n1 1\n255\n\x10"):
            with self.subTest(frame=invalid), self.assertRaises(ValueError):
                vision_describer.read_rgb(io.BytesIO(invalid))

    def test_shared_conversation_weights_must_match_the_signed_projector_pin(self):
        projector = {"vision_weights": "conversation", "vision_profile": "reference",
                     "conversation_sha256": "a" * 64}
        with patch.object(model_pack, "verify", side_effect=(projector, {"sha256": "b" * 64})) as verify:
            with self.assertRaisesRegex(ValueError, "different conversation weights"):
                vision_describer.signed_inputs()
        self.assertEqual([call.args[0] for call in verify.call_args_list], ["vision", "conversation"])
        self.assertEqual([call.kwargs["descriptor"] for call in verify.call_args_list],
                         [model_pack.descriptor_path("vision"), model_pack.DEFAULT_DESCRIPTOR])
        self.assertTrue(all(call.kwargs["provisional"] is False for call in verify.call_args_list))

    def test_dedicated_weights_are_selected_without_loading_the_conversation_pack(self):
        pack = {"vision_weights": "vision.gguf", "vision_profile": "small"}
        with patch.object(model_pack, "verify", return_value=pack) as verify:
            model, projector, profile = vision_describer.signed_inputs()
        verify.assert_called_once_with("vision", descriptor=model_pack.descriptor_path("vision"), provisional=False)
        self.assertEqual((model.name, projector.name, profile), ("vision.gguf", "mmproj.gguf", "small"))
