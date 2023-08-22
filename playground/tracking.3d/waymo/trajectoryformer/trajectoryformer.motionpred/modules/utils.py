import torch
import numpy as np
from copy import deepcopy


def filter_grads(parameters):
    return [param for param in parameters if param.requires_grad]


def rotate_points_along_z(points, angle):
    """
    Args:
        points: (B, N, 3 + C)
        angle: (B), angle along z-axis, angle increases x ==> y
    Returns:
    """
    cosa = torch.cos(angle)
    sina = torch.sin(angle)
    zeros = angle.new_zeros(points.shape[0])
    if points.shape[-1] == 2:
        rot_matrix = (
            torch.stack((cosa, sina, -sina, cosa), dim=1).view(-1, 2, 2).float()
        )
        points_rot = torch.matmul(points, rot_matrix)
    else:
        ones = angle.new_ones(points.shape[0])
        rot_matrix = (
            torch.stack(
                (cosa, sina, zeros, -sina, cosa, zeros, zeros, zeros, ones), dim=1
            )
            .view(-1, 3, 3)
            .float()
        )
        points_rot = torch.matmul(points[:, :, 0:3], rot_matrix)
        points_rot = torch.cat((points_rot, points[:, :, 3:]), dim=-1)
    return points_rot


def encode_boxes_res_torch(boxes, anchors):
    """
    Args:
        boxes: (N, 7 + C) [x, y, z, dx, dy, dz, heading, ...]
        anchors: (N, 7 + C) [x, y, z, dx, dy, dz, heading or *[cos, sin], ...]

    Returns:

    """
    anchors[:, 3:6] = torch.clamp_min(anchors[:, 3:6], min=1e-5)
    boxes[:, 3:6] = torch.clamp_min(boxes[:, 3:6], min=1e-5)

    xa, ya, za, dxa, dya, dza, ra, *cas = torch.split(anchors, 1, dim=-1)
    xg, yg, zg, dxg, dyg, dzg, rg, *cgs = torch.split(boxes, 1, dim=-1)

    diagonal = torch.sqrt(dxa**2 + dya**2)
    xt = (xg - xa) / diagonal
    yt = (yg - ya) / diagonal
    zt = (zg - za) / dza
    dxt = torch.log(dxg / dxa)
    dyt = torch.log(dyg / dya)
    dzt = torch.log(dzg / dza)
    encode_angle_by_sincos = False
    if encode_angle_by_sincos:
        rt_cos = torch.cos(rg) - torch.cos(ra)
        rt_sin = torch.sin(rg) - torch.sin(ra)
        rts = [rt_cos, rt_sin]
    else:
        rts = [rg - ra]

    cts = [g - a for g, a in zip(cgs, cas)]
    return torch.cat([xt, yt, zt, dxt, dyt, dzt, *rts, *cts], dim=-1)


def decode_torch(box_encodings, anchors):
    encode_angle_by_sincos = False
    xa, ya, za, dxa, dya, dza, ra, *cas = torch.split(anchors, 1, dim=-1)
    if not encode_angle_by_sincos:
        xt, yt, zt, dxt, dyt, dzt, rt, *cts = torch.split(box_encodings, 1, dim=-1)
    else:
        xt, yt, zt, dxt, dyt, dzt, cost, sint, *cts = torch.split(
            box_encodings, 1, dim=-1
        )

    diagonal = torch.sqrt(dxa**2 + dya**2)
    xg = xt * diagonal + xa
    yg = yt * diagonal + ya
    zg = zt * dza + za

    dxg = torch.exp(dxt) * dxa
    dyg = torch.exp(dyt) * dya
    dzg = torch.exp(dzt) * dza

    if encode_angle_by_sincos:
        rg_cos = cost + torch.cos(ra)
        rg_sin = sint + torch.sin(ra)
        rg = torch.atan2(rg_sin, rg_cos)
    else:
        rg = rt + ra

    cgs = [t + a for t, a in zip(cts, cas)]
    return torch.cat([xg, yg, zg, dxg, dyg, dzg, rg, *cgs], dim=-1)


def boxes_to_corners_3d(boxes3d):
    """
        7 -------- 4
       /|         /|
      6 -------- 5 .
      | |        | |
      . 3 -------- 0
      |/         |/
      2 -------- 1
    Args:
        boxes3d:  (N, 7) [x, y, z, dx, dy, dz, heading], (x, y, z) is the box center

    Returns:
    """
    # boxes3d, is_numpy = common_utils.check_numpy_to_torch(boxes3d)

    template = (
        boxes3d.new_tensor(
            (
                [1, 1, -1],
                [1, -1, -1],
                [-1, -1, -1],
                [-1, 1, -1],
                [1, 1, 1],
                [1, -1, 1],
                [-1, -1, 1],
                [-1, 1, 1],
            )
        )
        / 2
    )

    corners3d = boxes3d[:, None, 3:6].repeat(1, 8, 1) * template[None, :, :]
    corners3d = rotate_points_along_z(corners3d.view(-1, 8, 3), boxes3d[:, 6]).view(
        -1, 8, 3
    )
    corners3d += boxes3d[:, None, 0:3]

    return corners3d


def transform_trajs_to_local_coords(
    box_seq,
    center_xyz,
    center_heading,
    pred_vel_hypo=None,
    heading_index=8,
    rot_vel_index=[6, 7],
):
    box_seq_local = box_seq.clone()
    box_seq_local_buffer = torch.zeros_like(box_seq)
    valid_mask = torch.logical_and(
        (center_xyz[..., :2].sum(-1) != 0).repeat(1, box_seq.shape[1], 1, 1),
        box_seq[..., 3:6].sum(-1) != 0,
    )
    batch_size, len_traj, num_track, num_candi = (
        box_seq.shape[0],
        box_seq.shape[1],
        box_seq.shape[2],
        box_seq.shape[3],
    )
    box_seq_local[:, :, :, :, 0:2] = (
        box_seq_local[:, :, :, :, 0:2] - center_xyz[..., :2]
    )
    box_seq_local = rotate_points_along_z(
        points=box_seq_local.permute(0, 2, 3, 1, 4).reshape(
            batch_size * num_track * num_candi, -1, box_seq.shape[-1]
        ),
        angle=-center_heading.reshape(-1),
    )

    box_seq_local = box_seq_local.reshape(
        batch_size, num_track, num_candi, len_traj, -1
    ).permute(0, 3, 1, 2, 4)
    box_seq_local[:, :, :, :, heading_index] = (
        box_seq_local[:, :, :, :, heading_index] - center_heading
    )
    if pred_vel_hypo is not None:
        local_vel_buffer = torch.zeros_like(pred_vel_hypo)
        local_vel = rotate_points_along_z(
            points=pred_vel_hypo.permute(0, 2, 3, 1, 4).reshape(
                batch_size * num_track * num_candi, -1, pred_vel_hypo.shape[-1]
            ),
            angle=-center_heading.reshape(-1),
        )
        local_vel = local_vel.reshape(
            batch_size, num_track, num_candi, len_traj, -1
        ).permute(0, 3, 1, 2, 4)
        local_vel_buffer[valid_mask] = local_vel[valid_mask]
    else:
        local_vel_buffer = None

    box_seq_local_buffer[valid_mask] = box_seq_local[valid_mask]
    return box_seq_local_buffer, local_vel_buffer


def transform_trajs_to_global_coords(
    box_seq, center_xyz, center_heading, pred_vel_repeat=None, heading_index=6
):
    box_seq_local = box_seq.clone()
    batch_size, len_traj, num_track, num_candi = (
        box_seq.shape[0],
        box_seq.shape[1],
        box_seq.shape[2],
        box_seq.shape[3],
    )

    box_seq_local = rotate_points_along_z(
        points=box_seq_local.permute(0, 2, 3, 1, 4).reshape(
            batch_size * num_track * num_candi, -1, box_seq.shape[-1]
        ),
        angle=center_heading.reshape(-1),
    )
    box_seq_local = box_seq_local.reshape(
        batch_size, num_track, num_candi, len_traj, -1
    ).permute(0, 3, 1, 2, 4)
    box_seq_local[:, :, :, :, 0 : center_xyz.shape[-1]] = (
        box_seq_local[:, :, :, :, 0 : center_xyz.shape[-1]] + center_xyz
    )
    box_seq_local[:, :, :, :, heading_index] = (
        box_seq_local[:, :, :, :, heading_index] + center_heading
    )
    if pred_vel_repeat is not None:
        local_vel = rotate_points_along_z(
            points=pred_vel_repeat.permute(0, 2, 3, 1, 4).reshape(
                batch_size * num_track * num_candi, -1, pred_vel_repeat.shape[-1]
            ),
            angle=center_heading.reshape(-1),
        )
        local_vel = local_vel.reshape(
            batch_size, num_track, num_candi, len_traj, -1
        ).permute(0, 3, 1, 2, 4)
    else:
        local_vel = None

    return box_seq_local, local_vel


def get_corner_points(rois, batch_size_rcnn):
    faked_features = rois.new_ones((2, 2, 2))

    dense_idx = faked_features.nonzero()
    dense_idx = dense_idx.repeat(batch_size_rcnn, 1, 1).float()

    local_roi_size = rois.view(batch_size_rcnn, -1)[:, 3:6]
    roi_grid_points = dense_idx * local_roi_size.unsqueeze(dim=1) - (
        local_roi_size.unsqueeze(dim=1) / 2
    )
    return roi_grid_points


def get_corner_points_of_roi(rois):
    rois = rois.view(-1, rois.shape[-1])
    batch_size_rcnn = rois.shape[0]

    local_roi_grid_points = get_corner_points(rois, batch_size_rcnn)
    local_roi_grid_points = rotate_points_along_z(
        local_roi_grid_points.clone(), rois[:, 6]
    ).squeeze(dim=1)
    global_center = rois[:, 0:3].clone()

    global_roi_grid_points = local_roi_grid_points + global_center.unsqueeze(dim=1)
    return global_roi_grid_points, local_roi_grid_points


def spherical_coordinate(self, src, diag_dist):
    assert src.shape[-1] == 27
    device = src.device
    indices_x = torch.LongTensor([0, 3, 6, 9, 12, 15, 18, 21, 24]).to(device)  #
    indices_y = torch.LongTensor([1, 4, 7, 10, 13, 16, 19, 22, 25]).to(device)  #
    indices_z = torch.LongTensor([2, 5, 8, 11, 14, 17, 20, 23, 26]).to(device)
    src_x = torch.index_select(src, -1, indices_x)
    src_y = torch.index_select(src, -1, indices_y)
    src_z = torch.index_select(src, -1, indices_z)
    dis = (src_x**2 + src_y**2 + src_z**2) ** 0.5
    phi = torch.atan(src_y / (src_x + 1e-5))
    the = torch.acos(src_z / (dis + 1e-5))
    dis = dis / (diag_dist + 1e-5)
    src = torch.cat([dis, phi, the], dim=-1)
    return src


def reorder_rois(pred_bboxes):
    num_max_rois = max([len(bbox) for bbox in pred_bboxes])
    num_max_rois = max(1, num_max_rois)  # at least one faked rois to avoid error
    ordered_bboxes = torch.zeros(
        [len(pred_bboxes), num_max_rois, pred_bboxes[0].shape[-1]]
    ).cuda()
    valid_mask = torch.zeros(
        [len(pred_bboxes), num_max_rois, pred_bboxes[0].shape[-1]]
    ).cuda()
    for bs_idx in range(ordered_bboxes.shape[0]):
        ordered_bboxes[bs_idx, : len(pred_bboxes[bs_idx])] = pred_bboxes[bs_idx]
        valid_mask[bs_idx, : len(pred_bboxes[bs_idx])] = 1

    return ordered_bboxes, valid_mask.bool()


def crop_current_frame_points(num_lidar_points, trajectory_rois, points):
    batch_size, traj_length, num_track, candi_length, _ = trajectory_rois.shape
    src = torch.zeros(batch_size, num_track * candi_length, num_lidar_points, 6).cuda()
    trajectory_rois = trajectory_rois.reshape(batch_size, traj_length, -1, 8)
    num_rois = num_track * candi_length

    for bs_idx in range(batch_size):
        cur_batch_boxes = trajectory_rois[bs_idx, 0, :, :7].view(-1, 7)
        cur_radiis = (
            torch.sqrt(
                (cur_batch_boxes[:, 3] / 2) ** 2 + (cur_batch_boxes[:, 4] / 2) ** 2
            )
            * 1.2
        )
        cur_points = points[(points[:, 0] == bs_idx)][:, 1:]
        time_mask = cur_points[:, -1] < 1
        cur_points = cur_points[time_mask]
        # Slice to small batch to save GPU memory
        if cur_batch_boxes.shape[0] > 16:
            length_iter = cur_batch_boxes.shape[0] // 16
            dis_list = []
            for i in range(length_iter + 1):
                dis = torch.norm(
                    (
                        cur_points[:, :2].unsqueeze(0)
                        - cur_batch_boxes[16 * i : 16 * (i + 1), :2]
                        .unsqueeze(1)
                        .repeat(1, cur_points.shape[0], 1)
                    ),
                    dim=2,
                )
                dis_list.append(dis)
            dis = torch.cat(dis_list, 0)

        else:
            dis = torch.norm(
                (
                    cur_points[:, :2].unsqueeze(0)
                    - cur_batch_boxes[:, :2]
                    .unsqueeze(1)
                    .repeat(1, cur_points.shape[0], 1)
                ),
                dim=2,
            )

        point_mask = dis <= cur_radiis.unsqueeze(-1)

        for roi_box_idx in range(0, num_rois):
            cur_roi_points = cur_points[point_mask[roi_box_idx]]

            if cur_roi_points.shape[0] > num_lidar_points:
                np.random.seed(0)
                choice = np.random.choice(
                    cur_roi_points.shape[0], num_lidar_points, replace=False
                )
                cur_roi_points_sample = cur_roi_points[choice]

            elif cur_roi_points.shape[0] == 0:
                add_zeros = cur_roi_points.new_zeros(num_lidar_points, 6)
                add_zeros[:, :3] = trajectory_rois[bs_idx, 0:1, roi_box_idx, :3].repeat(
                    num_lidar_points, 1
                )
                cur_roi_points_sample = add_zeros

            else:
                empty_num = num_lidar_points - cur_roi_points.shape[0]
                add_zeros = cur_roi_points[0].repeat(empty_num, 1)
                cur_roi_points_sample = torch.cat([cur_roi_points, add_zeros], dim=0)

            src[bs_idx, roi_box_idx, :num_lidar_points, :] = cur_roi_points_sample

    return src


def transform_box_to_global(pred_boxes3d, pred_vels, pose):
    expand_bboxes = np.concatenate(
        [pred_boxes3d[:, :3], np.ones((pred_boxes3d.shape[0], 1))], axis=-1
    )
    expand_vels = np.concatenate(
        [pred_vels[:, 0:2], np.zeros((pred_boxes3d.shape[0], 1))], axis=-1
    )
    bboxes_global = np.dot(expand_bboxes, pose.T)[:, :3]
    vels_global = np.dot(expand_vels, pose[:3, :3].T)
    moved_bboxes_global = deepcopy(bboxes_global)
    bboxes_pre2cur = np.concatenate(
        [moved_bboxes_global, pred_boxes3d[:, 3:7]], axis=-1
    )
    bboxes_pre2cur[..., -1] = bboxes_pre2cur[..., -1] + np.arctan2(
        pose[..., 1, 0], pose[..., 0, 0]
    )

    return (
        torch.tensor(bboxes_pre2cur).cuda().float(),
        torch.tensor(vels_global[:, :2]).cuda().float(),
    )
