"""Directory sources ignore the common '.tif' TIFF extension (only '.tiff' listed)."""

import cv2
import numpy as np
from gptme_vision_node.frame_source import ImageFileSource


def test_directory_of_tif_images_is_served(tmp_path):
    f = np.full((16, 16, 3), 100, np.uint8)
    assert cv2.imwrite(str(tmp_path / "frame0.tif"), f)
    src = ImageFileSource(tmp_path)  # raises "no images found" on master
    assert src.get_frame() is not None
