from pathlib import Path
from typing import Dict, Final, List, Optional, Union

import cv2
import numpy as np
import onnxruntime
from huggingface_hub import hf_hub_download
from PIL import Image

from unstructured_inference.inference.layoutelement import LayoutElement
from unstructured_inference.logger import logger, logger_onnx
from unstructured_inference.models.unstructuredmodel import (
    UnstructuredObjectDetectionModel,
)
from unstructured_inference.utils import LazyDict, LazyEvaluateInfo

onnxruntime.set_default_logger_severity(logger_onnx.getEffectiveLevel())

DEFAULT_LABEL_MAP: Final[Dict[int, str]] = {
    0: "Text",
    1: "Title",
    2: "List",
    3: "Table",
    4: "Figure",
}


# NOTE(alan): Entries are implemented as LazyDicts so that models aren't downloaded until they are
# needed.
MODEL_TYPES: Dict[Optional[str], LazyDict] = {
    "detectron2_onnx": LazyDict(
        model_path=LazyEvaluateInfo(
            hf_hub_download,
            "unstructuredio/detectron2_faster_rcnn_R_50_FPN_3x",
            "model.onnx",
        ),
        label_map=DEFAULT_LABEL_MAP,
        confidence_threshold=0.8,
    ),
}


class UnstructuredDetectronONNXModel(UnstructuredObjectDetectionModel):
    """Unstructured model wrapper for detectron2 ONNX model."""

    # The model was trained and exported with this shape
    required_w = 800
    required_h = 1035

    def predict(self, image: Image.Image) -> List[LayoutElement]:
        """Makes a prediction using detectron2 model."""
        super().predict(image)

        prepared_input = self.preprocess(image)
        try:
            bboxes, labels, confidence_scores, _ = self.model.run(None, prepared_input)
        except onnxruntime.capi.onnxruntime_pybind11_state.RuntimeException:
            logger_onnx.debug(
                "Ignoring runtime error from onnx (likely due to encountering blank page).",
            )
            return []
        input_w, input_h = image.size
        regions = self.postprocess(bboxes, labels, confidence_scores, input_w, input_h)

        return regions

    def initialize(
        self,
        model_path: Union[str, Path],
        label_map: Dict[int, str],
        confidence_threshold: Optional[float] = None,
    ):
        """Loads the detectron2 model using the specified parameters"""
        logger.info("Loading the Detectron2 layout model ...")
        self.model = onnxruntime.InferenceSession(
            model_path,
            providers=[
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            ],
        )
        self.label_map = label_map
        if confidence_threshold is None:
            confidence_threshold = 0.5
        self.confidence_threshold = confidence_threshold

    def preprocess(self, image: Image.Image) -> Dict[str, np.ndarray]:
        """Process input image into required format for ingestion into the Detectron2 ONNX binary.
        This involves resizing to a fixed shape and converting to a specific numpy format."""
        # TODO (benjamin): check other shapes for inference
        img = np.array(image)
        # TODO (benjamin): We should use models.get_model() but currenly returns Detectron model
        session = self.model
        # onnx input expected
        # [3,1035,800]
        img = cv2.resize(
            img,
            (self.required_w, self.required_h),
            interpolation=cv2.INTER_LINEAR,
        ).astype(np.float32)
        img = img.transpose(2, 0, 1)
        ort_inputs = {session.get_inputs()[0].name: img}
        return ort_inputs

    def postprocess(
        self,
        bboxes: np.ndarray,
        labels: np.ndarray,
        confidence_scores: np.ndarray,
        input_w: float,
        input_h: float,
    ) -> List[LayoutElement]:
        """Process output into Unstructured class. Bounding box coordinates are converted to
        original image resolution."""
        regions = []
        width_conversion = input_w / self.required_w
        height_conversion = input_h / self.required_h
        for (x1, y1, x2, y2), label, conf in zip(bboxes, labels, confidence_scores):
            detected_class = self.label_map[int(label)]
            if conf >= self.confidence_threshold:
                region = LayoutElement(
                    x1 * width_conversion,
                    y1 * height_conversion,
                    x2 * width_conversion,
                    y2 * height_conversion,
                    text=None,
                    type=detected_class,
                )

                regions.append(region)

        regions.sort(key=lambda element: element.y1)
        return regions
