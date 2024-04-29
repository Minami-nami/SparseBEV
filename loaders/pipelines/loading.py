import os
import mmcv
import numpy as np
from mmdet.datasets.builder import PIPELINES
from numpy.linalg import inv
from mmcv.runner import get_dist_info


def compose_lidar2img(
    ego2global_translation_curr,
    ego2global_rotation_curr,
    lidar2ego_translation_curr,
    lidar2ego_rotation_curr,
    sensor2global_translation_past,
    sensor2global_rotation_past,
    cam_intrinsic_past,
):
    R = sensor2global_rotation_past @ (
        inv(ego2global_rotation_curr).T @ inv(lidar2ego_rotation_curr).T
    )
    T = sensor2global_translation_past @ (
        inv(ego2global_rotation_curr).T @ inv(lidar2ego_rotation_curr).T
    )
    T -= (
        ego2global_translation_curr
        @ (inv(ego2global_rotation_curr).T @ inv(lidar2ego_rotation_curr).T)
        + lidar2ego_translation_curr @ inv(lidar2ego_rotation_curr).T
    )

    lidar2cam_r = inv(R.T)
    lidar2cam_t = T @ lidar2cam_r.T

    lidar2cam_rt = np.eye(4)
    lidar2cam_rt[:3, :3] = lidar2cam_r.T
    lidar2cam_rt[3, :3] = -lidar2cam_t

    viewpad = np.eye(4)
    viewpad[: cam_intrinsic_past.shape[0], : cam_intrinsic_past.shape[1]] = (
        cam_intrinsic_past
    )
    lidar2img = (viewpad @ lidar2cam_rt.T).astype(np.float32)

    return lidar2img


@PIPELINES.register_module()
class CustomLoadMultiViewImageFromFiles(object):
    """Load multi channel images from a list of separate channel files.

    Expects results['img_filename'] to be a list of filenames.

    Args:
        to_float32 (bool, optional): Whether to convert the img to float32.
            Defaults to False.
        color_type (str, optional): Color type of the file.
            Defaults to 'unchanged'.
    """

    def __init__(self, to_float32=False, color_type="unchanged", num_views=6):
        self.to_float32 = to_float32
        self.color_type = color_type
        self.num_views = num_views

    def __call__(self, results):
        """Call function to load multi-view image from files.

        Args:
            results (dict): Result dict containing multi-view image filenames.

        Returns:
            dict: The result dict containing the multi-view image data.
                Added keys and values are described below.

                - filename (str): Multi-view image filenames.
                - img (np.ndarray): Multi-view image arrays.
                - img_shape (tuple[int]): Shape of multi-view image arrays.
                - ori_shape (tuple[int]): Shape of original image arrays.
                - pad_shape (tuple[int]): Shape of padded image arrays.
                - scale_factor (float): Scale factor.
                - img_norm_cfg (dict): Normalization configuration of images.
        """
        filename = results["img_filename"][: self.num_views]
        # img is of shape (h, w, c, num_views)
        img = np.stack(
            [mmcv.imread(name, self.color_type) for name in filename], axis=-1
        )
        if self.to_float32:
            img = img.astype(np.float32)
        results["filename"] = filename
        # unravel to list, see `DefaultFormatBundle` in formatting.py
        # which will transpose each image separately and then stack into array
        results["img"] = [img[..., i] for i in range(img.shape[-1])]
        results["img_shape"] = img.shape
        results["ori_shape"] = img.shape
        # Set initial values for default meta_keys
        results["pad_shape"] = img.shape
        results["scale_factor"] = 1.0
        num_channels = 1 if len(img.shape) < 3 else img.shape[2]
        results["img_norm_cfg"] = dict(
            mean=np.zeros(num_channels, dtype=np.float32),
            std=np.ones(num_channels, dtype=np.float32),
            to_rgb=False,
        )
        return results

    def __repr__(self):
        """str: Return a string that describes the module."""
        repr_str = self.__class__.__name__
        repr_str += f"(to_float32={self.to_float32}, "
        repr_str += f"color_type='{self.color_type}')"
        return repr_str


@PIPELINES.register_module()
class LoadMultiViewImageFromMultiSweeps(object):
    def __init__(self, sweeps_num=5, color_type="color", test_mode=False, num_views=6):
        self.sweeps_num = sweeps_num
        self.color_type = color_type
        self.test_mode = test_mode
        self.num_views = num_views

        self.train_interval = [4, 8]
        self.test_interval = 6

        try:
            mmcv.use_backend("turbojpeg")
        except ImportError:
            mmcv.use_backend("cv2")

    def load_offline(self, results):
        cam_types = [
            "CAM_FRONT",
            "CAM_FRONT_RIGHT",
            "CAM_FRONT_LEFT",
            "CAM_BACK",
            "CAM_BACK_LEFT",
            "CAM_BACK_RIGHT",
        ]

        if len(results["sweeps"]["prev"]) == 0:
            for _ in range(self.sweeps_num):
                for j in range(len(cam_types)):
                    results["img"].append(results["img"][j])
                    results["img_timestamp"].append(results["img_timestamp"][j])
                    results["filename"].append(results["filename"][j])
                    results["lidar2img"].append(np.copy(results["lidar2img"][j]))
        else:
            if self.test_mode:
                interval = self.test_interval
                choices = [(k + 1) * interval - 1 for k in range(self.sweeps_num)]
            elif len(results["sweeps"]["prev"]) <= self.sweeps_num:
                pad_len = self.sweeps_num - len(results["sweeps"]["prev"])
                choices = (
                    list(range(len(results["sweeps"]["prev"])))
                    + [len(results["sweeps"]["prev"]) - 1] * pad_len
                )
            else:
                max_interval = len(results["sweeps"]["prev"]) // self.sweeps_num
                max_interval = min(max_interval, self.train_interval[1])
                min_interval = min(max_interval, self.train_interval[0])
                interval = np.random.randint(min_interval, max_interval + 1)
                choices = [(k + 1) * interval - 1 for k in range(self.sweeps_num)]

            for idx in sorted(list(choices)):
                sweep_idx = min(idx, len(results["sweeps"]["prev"]) - 1)
                sweep = results["sweeps"]["prev"][sweep_idx]

                if len(sweep.keys()) < len(cam_types):
                    sweep = results["sweeps"]["prev"][sweep_idx - 1]

                for sensor in cam_types:
                    results["img"].append(
                        mmcv.imread(sweep[sensor]["data_path"], self.color_type)
                    )
                    results["img_timestamp"].append(sweep[sensor]["timestamp"] / 1e6)
                    results["filename"].append(
                        os.path.relpath(sweep[sensor]["data_path"])
                    )
                    results["lidar2img"].append(
                        compose_lidar2img(
                            results["ego2global_translation"],
                            results["ego2global_rotation"],
                            results["lidar2ego_translation"],
                            results["lidar2ego_rotation"],
                            sweep[sensor]["sensor2global_translation"],
                            sweep[sensor]["sensor2global_rotation"],
                            sweep[sensor]["cam_intrinsic"],
                        )
                    )

        return results

    def load_online(self, results):
        # only used when measuring FPS
        assert self.test_mode
        assert self.test_interval == 6
        assert self.num_views <= 6 and self.num_views >= 1

        cam_types = [
            "CAM_FRONT",
            "CAM_FRONT_RIGHT",
            "CAM_FRONT_LEFT",
            "CAM_BACK",
            "CAM_BACK_LEFT",
            "CAM_BACK_RIGHT",
        ]

        cam_types = cam_types[: self.num_views]
        # results["filename"] = results["filename"][: self.num_views]
        results["lidar2img"] = results["lidar2img"][: self.num_views]
        results["img_timestamp"] = results["img_timestamp"][: self.num_views]
        # results["img"] = results["img"][: self.num_views]
        # results["img_shape"] = results["ori_shape"] = results["pad_shape"] = results["img_shape"][:-1] + (self.num_views,)

        if len(results["sweeps"]["prev"]) == 0:
            for _ in range(self.sweeps_num):
                for j in range(len(cam_types)):
                    results["img_timestamp"].append(results["img_timestamp"][j])
                    results["filename"].append(results["filename"][j])
                    results["lidar2img"].append(np.copy(results["lidar2img"][j]))
        else:
            interval = self.test_interval
            choices = [(k + 1) * interval - 1 for k in range(self.sweeps_num)]

            for idx in sorted(list(choices)):
                sweep_idx = min(idx, len(results["sweeps"]["prev"]) - 1)
                sweep = results["sweeps"]["prev"][sweep_idx]

                if len(sweep.keys()) < len(cam_types):
                    sweep = results["sweeps"]["prev"][sweep_idx - 1]

                for sensor in cam_types:
                    # skip loading history frames
                    results["img_timestamp"].append(sweep[sensor]["timestamp"] / 1e6)
                    results["filename"].append(
                        os.path.relpath(sweep[sensor]["data_path"])
                    )
                    results["lidar2img"].append(
                        compose_lidar2img(
                            results["ego2global_translation"],
                            results["ego2global_rotation"],
                            results["lidar2ego_translation"],
                            results["lidar2ego_rotation"],
                            sweep[sensor]["sensor2global_translation"],
                            sweep[sensor]["sensor2global_rotation"],
                            sweep[sensor]["cam_intrinsic"],
                        )
                    )

        return results

    def __call__(self, results):
        if self.sweeps_num == 0:
            return results

        world_size = get_dist_info()[1]
        if world_size == 1 and self.test_mode:
            return self.load_online(results)
        else:
            return self.load_offline(results)


@PIPELINES.register_module()
class LoadMultiViewImageFromMultiSweepsFuture(object):
    def __init__(
        self, prev_sweeps_num=5, next_sweeps_num=5, color_type="color", test_mode=False
    ):
        self.prev_sweeps_num = prev_sweeps_num
        self.next_sweeps_num = next_sweeps_num
        self.color_type = color_type
        self.test_mode = test_mode

        assert prev_sweeps_num == next_sweeps_num

        self.train_interval = [4, 8]
        self.test_interval = 6

        try:
            mmcv.use_backend("turbojpeg")
        except ImportError:
            mmcv.use_backend("cv2")

    def __call__(self, results):
        if self.prev_sweeps_num == 0 and self.next_sweeps_num == 0:
            return results

        cam_types = [
            "CAM_FRONT",
            "CAM_FRONT_RIGHT",
            "CAM_FRONT_LEFT",
            "CAM_BACK",
            "CAM_BACK_LEFT",
            "CAM_BACK_RIGHT",
        ]

        if self.test_mode:
            interval = self.test_interval
        else:
            interval = np.random.randint(
                self.train_interval[0], self.train_interval[1] + 1
            )

        # previous sweeps
        if len(results["sweeps"]["prev"]) == 0:
            for _ in range(self.prev_sweeps_num):
                for j in range(len(cam_types)):
                    results["img"].append(results["img"][j])
                    results["img_timestamp"].append(results["img_timestamp"][j])
                    results["filename"].append(results["filename"][j])
                    results["lidar2img"].append(np.copy(results["lidar2img"][j]))
        else:
            choices = [(k + 1) * interval - 1 for k in range(self.prev_sweeps_num)]

            for idx in sorted(list(choices)):
                sweep_idx = min(idx, len(results["sweeps"]["prev"]) - 1)
                sweep = results["sweeps"]["prev"][sweep_idx]

                if len(sweep.keys()) < len(cam_types):
                    sweep = results["sweeps"]["prev"][sweep_idx - 1]

                for sensor in cam_types:
                    results["img"].append(
                        mmcv.imread(sweep[sensor]["data_path"], self.color_type)
                    )
                    results["img_timestamp"].append(sweep[sensor]["timestamp"] / 1e6)
                    results["filename"].append(sweep[sensor]["data_path"])
                    results["lidar2img"].append(
                        compose_lidar2img(
                            results["ego2global_translation"],
                            results["ego2global_rotation"],
                            results["lidar2ego_translation"],
                            results["lidar2ego_rotation"],
                            sweep[sensor]["sensor2global_translation"],
                            sweep[sensor]["sensor2global_rotation"],
                            sweep[sensor]["cam_intrinsic"],
                        )
                    )

        # future sweeps
        if len(results["sweeps"]["next"]) == 0:
            for _ in range(self.next_sweeps_num):
                for j in range(len(cam_types)):
                    results["img"].append(results["img"][j])
                    results["img_timestamp"].append(results["img_timestamp"][j])
                    results["filename"].append(results["filename"][j])
                    results["lidar2img"].append(np.copy(results["lidar2img"][j]))
        else:
            choices = [(k + 1) * interval - 1 for k in range(self.next_sweeps_num)]

            for idx in sorted(list(choices)):
                sweep_idx = min(idx, len(results["sweeps"]["next"]) - 1)
                sweep = results["sweeps"]["next"][sweep_idx]

                if len(sweep.keys()) < len(cam_types):
                    sweep = results["sweeps"]["next"][sweep_idx - 1]

                for sensor in cam_types:
                    results["img"].append(
                        mmcv.imread(sweep[sensor]["data_path"], self.color_type)
                    )
                    results["img_timestamp"].append(sweep[sensor]["timestamp"] / 1e6)
                    results["filename"].append(sweep[sensor]["data_path"])
                    results["lidar2img"].append(
                        compose_lidar2img(
                            results["ego2global_translation"],
                            results["ego2global_rotation"],
                            results["lidar2ego_translation"],
                            results["lidar2ego_rotation"],
                            sweep[sensor]["sensor2global_translation"],
                            sweep[sensor]["sensor2global_rotation"],
                            sweep[sensor]["cam_intrinsic"],
                        )
                    )

        return results


"""
This func loads previous and future frames in interleaved order, 
e.g. curr, prev1, next1, prev2, next2, prev3, next3...
"""


@PIPELINES.register_module()
class LoadMultiViewImageFromMultiSweepsFutureInterleave(object):
    def __init__(
        self, prev_sweeps_num=5, next_sweeps_num=5, color_type="color", test_mode=False
    ):
        self.prev_sweeps_num = prev_sweeps_num
        self.next_sweeps_num = next_sweeps_num
        self.color_type = color_type
        self.test_mode = test_mode

        assert prev_sweeps_num == next_sweeps_num

        self.train_interval = [4, 8]
        self.test_interval = 6

        try:
            mmcv.use_backend("turbojpeg")
        except ImportError:
            mmcv.use_backend("cv2")

    def __call__(self, results):
        if self.prev_sweeps_num == 0 and self.next_sweeps_num == 0:
            return results

        cam_types = [
            "CAM_FRONT",
            "CAM_FRONT_RIGHT",
            "CAM_FRONT_LEFT",
            "CAM_BACK",
            "CAM_BACK_LEFT",
            "CAM_BACK_RIGHT",
        ]

        if self.test_mode:
            interval = self.test_interval
        else:
            interval = np.random.randint(
                self.train_interval[0], self.train_interval[1] + 1
            )

        results_prev = dict(img=[], img_timestamp=[], filename=[], lidar2img=[])
        results_next = dict(img=[], img_timestamp=[], filename=[], lidar2img=[])

        if len(results["sweeps"]["prev"]) == 0:
            for _ in range(self.prev_sweeps_num):
                for j in range(len(cam_types)):
                    results_prev["img"].append(results["img"][j])
                    results_prev["img_timestamp"].append(results["img_timestamp"][j])
                    results_prev["filename"].append(results["filename"][j])
                    results_prev["lidar2img"].append(np.copy(results["lidar2img"][j]))
        else:
            choices = [(k + 1) * interval - 1 for k in range(self.prev_sweeps_num)]

            for idx in sorted(list(choices)):
                sweep_idx = min(idx, len(results["sweeps"]["prev"]) - 1)
                sweep = results["sweeps"]["prev"][sweep_idx]

                if len(sweep.keys()) < len(cam_types):
                    sweep = results["sweeps"]["prev"][sweep_idx - 1]

                for sensor in cam_types:
                    results_prev["img"].append(
                        mmcv.imread(sweep[sensor]["data_path"], self.color_type)
                    )
                    results_prev["img_timestamp"].append(
                        sweep[sensor]["timestamp"] / 1e6
                    )
                    results_prev["filename"].append(
                        os.path.relpath(sweep[sensor]["data_path"])
                    )
                    results_prev["lidar2img"].append(
                        compose_lidar2img(
                            results["ego2global_translation"],
                            results["ego2global_rotation"],
                            results["lidar2ego_translation"],
                            results["lidar2ego_rotation"],
                            sweep[sensor]["sensor2global_translation"],
                            sweep[sensor]["sensor2global_rotation"],
                            sweep[sensor]["cam_intrinsic"],
                        )
                    )

        if len(results["sweeps"]["next"]) == 0:
            print(1, len(results_next["img"]))
            for _ in range(self.next_sweeps_num):
                for j in range(len(cam_types)):
                    results_next["img"].append(results["img"][j])
                    results_next["img_timestamp"].append(results["img_timestamp"][j])
                    results_next["filename"].append(results["filename"][j])
                    results_next["lidar2img"].append(np.copy(results["lidar2img"][j]))
        else:
            choices = [(k + 1) * interval - 1 for k in range(self.next_sweeps_num)]

            for idx in sorted(list(choices)):
                sweep_idx = min(idx, len(results["sweeps"]["next"]) - 1)
                sweep = results["sweeps"]["next"][sweep_idx]

                if len(sweep.keys()) < len(cam_types):
                    sweep = results["sweeps"]["next"][sweep_idx - 1]

                for sensor in cam_types:
                    results_next["img"].append(
                        mmcv.imread(sweep[sensor]["data_path"], self.color_type)
                    )
                    results_next["img_timestamp"].append(
                        sweep[sensor]["timestamp"] / 1e6
                    )
                    results_next["filename"].append(
                        os.path.relpath(sweep[sensor]["data_path"])
                    )
                    results_next["lidar2img"].append(
                        compose_lidar2img(
                            results["ego2global_translation"],
                            results["ego2global_rotation"],
                            results["lidar2ego_translation"],
                            results["lidar2ego_rotation"],
                            sweep[sensor]["sensor2global_translation"],
                            sweep[sensor]["sensor2global_rotation"],
                            sweep[sensor]["cam_intrinsic"],
                        )
                    )

        assert len(results_prev["img"]) % 6 == 0
        assert len(results_next["img"]) % 6 == 0

        for i in range(len(results_prev["img"]) // 6):
            for j in range(6):
                results["img"].append(results_prev["img"][i * 6 + j])
                results["img_timestamp"].append(
                    results_prev["img_timestamp"][i * 6 + j]
                )
                results["filename"].append(results_prev["filename"][i * 6 + j])
                results["lidar2img"].append(results_prev["lidar2img"][i * 6 + j])

            for j in range(6):
                results["img"].append(results_next["img"][i * 6 + j])
                results["img_timestamp"].append(
                    results_next["img_timestamp"][i * 6 + j]
                )
                results["filename"].append(results_next["filename"][i * 6 + j])
                results["lidar2img"].append(results_next["lidar2img"][i * 6 + j])

        return results
