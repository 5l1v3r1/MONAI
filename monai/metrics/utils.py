# Copyright 2020 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Tuple, Union

import numpy as np
import torch

from monai.transforms.croppad.array import SpatialCrop
from monai.transforms.utils import generate_spatial_bounding_box
from monai.utils import MetricReduction, optional_import

binary_erosion, _ = optional_import("scipy.ndimage.morphology", name="binary_erosion")
distance_transform_edt, _ = optional_import("scipy.ndimage.morphology", name="distance_transform_edt")
distance_transform_cdt, _ = optional_import("scipy.ndimage.morphology", name="distance_transform_cdt")


def ignore_background(
    y_pred: torch.Tensor,
    y: torch.Tensor,
):
    """
    This function is used to remove background (the first channel) for `y_pred` and `y`.
    Args:
        y_pred: predictions. As for classification tasks,
            `y_pred` should has the shape [BN] where N is larger than 1. As for segmentation tasks,
            the shape should be [BNHW] or [BNHWD].
        y: ground truth, the first dim is batch.

    """
    y = y[:, 1:] if y.shape[1] > 1 else y
    y_pred = y_pred[:, 1:] if y_pred.shape[1] > 1 else y_pred
    return y_pred, y


def do_metric_reduction(
    f: torch.Tensor,
    reduction: Union[MetricReduction, str] = MetricReduction.MEAN,
):
    """
    This function is to do the metric reduction for calculated metrics of each example's each class.
    Args:
        f: a tensor that contains the calculated metric scores per batch and
            per class. The first two dims should be batch and class.
        reduction: {``"none"``, ``"mean"``, ``"sum"``, ``"mean_batch"``, ``"sum_batch"``,
        ``"mean_channel"``, ``"sum_channel"``}
        Define the mode to reduce computation result of 1 batch data. Defaults to ``"mean"``.

    Raises:
        ValueError: When ``reduction`` is not one of
            ["mean", "sum", "mean_batch", "sum_batch", "mean_channel", "sum_channel" "none"].
    """

    # some elements might be Nan (if ground truth y was missing (zeros))
    # we need to account for it
    nans = torch.isnan(f)
    not_nans = (~nans).float()
    f[nans] = 0

    t_zero = torch.zeros(1, device=f.device, dtype=torch.float)
    reduction = MetricReduction(reduction)

    if reduction == MetricReduction.MEAN:
        # 2 steps, first, mean by channel (accounting for nans), then by batch
        not_nans = not_nans.sum(dim=1)
        f = torch.where(not_nans > 0, f.sum(dim=1) / not_nans, t_zero)  # channel average

        not_nans = (not_nans > 0).float().sum(dim=0)
        f = torch.where(not_nans > 0, f.sum(dim=0) / not_nans, t_zero)  # batch average

    elif reduction == MetricReduction.SUM:
        not_nans = not_nans.sum(dim=[0, 1])
        f = torch.sum(f, dim=[0, 1])  # sum over the batch and channel dims
    elif reduction == MetricReduction.MEAN_BATCH:
        not_nans = not_nans.sum(dim=0)
        f = torch.where(not_nans > 0, f.sum(dim=0) / not_nans, t_zero)  # batch average
    elif reduction == MetricReduction.SUM_BATCH:
        not_nans = not_nans.sum(dim=0)
        f = f.sum(dim=0)  # the batch sum
    elif reduction == MetricReduction.MEAN_CHANNEL:
        not_nans = not_nans.sum(dim=1)
        f = torch.where(not_nans > 0, f.sum(dim=1) / not_nans, t_zero)  # channel average
    elif reduction == MetricReduction.SUM_CHANNEL:
        not_nans = not_nans.sum(dim=1)
        f = f.sum(dim=1)  # the channel sum
    elif reduction == MetricReduction.NONE:
        pass
    else:
        raise ValueError(
            f"Unsupported reduction: {reduction}, available options are "
            '["mean", "sum", "mean_batch", "sum_batch", "mean_channel", "sum_channel" "none"].'
        )
    return f, not_nans


def get_mask_edges(
    seg_pred: Union[np.ndarray, torch.Tensor],
    seg_gt: Union[np.ndarray, torch.Tensor],
    label_idx: int,
    crop: bool = True,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Do binary erosion and use XOR for input to get the edges. This
    function is helpful to further calculate metrics such as Average Surface
    Distance and Hausdorff Distance.
    The input images can be binary or labelfield images. If labelfield images
    are supplied, they are converted to binary images using `label_idx`.

    `scipy`'s binary erosion is used to to calculate the edges of the binary
    labelfield.

    In order to improve the computing efficiency, before getting the edges,
    the images can be cropped and only keep the foreground if not specifies
    ``crop = False``.

    We require that images are the same size, and assume that they occupy the
    same space (spacing, orientation, etc.).

    Args:
        seg_pred: the predicted binary or labelfield image.
        seg_gt: the actual binary or labelfield image.
        label_idx: for labelfield images, convert to binary with
            `seg_pred = seg_pred == label_idx`.
        crop: crop input images and only keep the foregrounds. In order to
            maintain two inputs' shapes, here the bounding box is achieved
            by ``(seg_pred | seg_gt)`` which represents the union set of two
            images. Defaults to ``True``.
    """

    # Get both labelfields as np arrays
    if torch.is_tensor(seg_pred):
        seg_pred = seg_pred.detach().cpu().numpy()
    if torch.is_tensor(seg_gt):
        seg_gt = seg_gt.detach().cpu().numpy()

    # Check non-zero number of elements and same shape
    if seg_pred.size == 0 or seg_pred.shape != seg_gt.shape:
        raise ValueError("Labelfields should have same shape (and non-zero number of elements)")

    # If not binary images, convert them
    if seg_pred.dtype != bool:
        seg_pred = seg_pred == label_idx
    if seg_gt.dtype != bool:
        seg_gt = seg_gt == label_idx

    if crop:
        if not np.any(seg_pred | seg_gt):
            return (np.zeros_like(seg_pred), np.zeros_like(seg_gt))

        seg_pred, seg_gt = np.expand_dims(seg_pred, 0), np.expand_dims(seg_gt, 0)
        box_start, box_end = generate_spatial_bounding_box(seg_pred | seg_gt)
        cropper = SpatialCrop(roi_start=box_start, roi_end=box_end)
        seg_pred, seg_gt = np.squeeze(cropper(seg_pred)), np.squeeze(cropper(seg_gt))

    # Do binary erosion and use XOR to get edges
    edges_pred = binary_erosion(seg_pred) ^ seg_pred
    edges_gt = binary_erosion(seg_gt) ^ seg_gt

    return (edges_pred, edges_gt)


def get_surface_distance(
    edges_pred: np.ndarray,
    edges_gt: np.ndarray,
    label_idx: int,
    crop: bool = True,
    distance_metric: str = "euclidean",
) -> np.ndarray:
    """
    This function is used to compute the surface distances from `seg_pred` to `seg_gt`.

    In order to improve the computing efficiency, before getting the edges,
    the images can be cropped and only keep the foreground if not specifies
    ``crop = False``.

    Args:
        edges_pred: the edge of the predictions.
        edges_gt: the edge of the ground truth.
        label_idx: for labelfield images, convert to binary with
            `seg_pred = seg_pred == label_idx`.
        crop: crop input images and only keep the foregrounds. In order to
            maintain two inputs' shapes, here the bounding box is achieved
            by ``(seg_pred | seg_gt)`` which represents the union set of two
            images. Defaults to ``True``.
        distance_metric: : [``"euclidean"``, ``"chessboard"``, ``"taxicab"``]
            the metric used to compute surface distance. Defaults to ``"euclidean"``.

            - ``"euclidean"``, uses Exact Euclidean distance transform.
            - ``"chessboard"``, uses `chessboard` metric in chamfer type of transform.
            - ``"taxicab"``, uses `taxicab` metric in chamfer type of transform.
    """

    if not np.any(edges_pred):
        return np.array([])

    if not np.any(edges_gt):
        dis = np.inf * np.ones_like(edges_gt)
    else:
        if distance_metric == "euclidean":
            dis = distance_transform_edt(~edges_gt)
        elif distance_metric == "chessboard" or distance_metric == "taxicab":
            dis = distance_transform_cdt(~edges_gt, metric=distance_metric)
        else:
            raise ValueError(f"distance_metric {distance_metric} is not implemented.")
    surface_distance = dis[edges_pred]
    return surface_distance
