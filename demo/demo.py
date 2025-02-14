from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse

import torch
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision
import cv2
import numpy as np
import os

import _init_paths
import models
from config import cfg
from config import update_config
from core.function import get_final_preds
from utils.transforms import get_affine_transform
import pose_estimation.sort as Sort

import os 
cur_dir = os.path.dirname(os.path.realpath(__file__))

COCO_KEYPOINT_INDEXES = {
    0: 'nose',
    1: 'left_eye',
    2: 'right_eye',
    3: 'left_ear',
    4: 'right_ear',
    5: 'left_shoulder',
    6: 'right_shoulder',
    7: 'left_elbow',
    8: 'right_elbow',
    9: 'left_wrist',
    10: 'right_wrist',
    11: 'left_hip',
    12: 'right_hip',
    13: 'left_knee',
    14: 'right_knee',
    15: 'left_ankle',
    16: 'right_ankle'
}

COCO_INSTANCE_CATEGORY_NAMES = [
    '__background__', 'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus',
    'train', 'truck', 'boat', 'traffic light', 'fire hydrant', 'N/A', 'stop sign',
    'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep', 'cow',
    'elephant', 'bear', 'zebra', 'giraffe', 'N/A', 'backpack', 'umbrella', 'N/A', 'N/A',
    'handbag', 'tie', 'suitcase', 'frisbee', 'skis', 'snowboard', 'sports ball',
    'kite', 'baseball bat', 'baseball glove', 'skateboard', 'surfboard', 'tennis racket',
    'bottle', 'N/A', 'wine glass', 'cup', 'fork', 'knife', 'spoon', 'bowl',
    'banana', 'apple', 'sandwich', 'orange', 'broccoli', 'carrot', 'hot dog', 'pizza',
    'donut', 'cake', 'chair', 'couch', 'potted plant', 'bed', 'N/A', 'dining table',
    'N/A', 'N/A', 'toilet', 'N/A', 'tv', 'laptop', 'mouse', 'remote', 'keyboard', 'cell phone',
    'microwave', 'oven', 'toaster', 'sink', 'refrigerator', 'N/A', 'book',
    'clock', 'vase', 'scissors', 'teddy bear', 'hair drier', 'toothbrush'
]

SKELETON = [
    [1,3],[1,0],[2,4],[2,0],[0,5],[0,6],[5,7],[7,9],[6,8],[8,10],[5,11],[6,12],[11,12],[11,13],[13,15],[12,14],[14,16]
]

CocoColors = [[255, 0, 0], [255, 85, 0], [255, 170, 0], [255, 255, 0], [170, 255, 0], [85, 255, 0], [0, 255, 0],
              [0, 255, 85], [0, 255, 170], [0, 255, 255], [0, 170, 255], [0, 85, 255], [0, 0, 255], [85, 0, 255],
              [170, 0, 255], [255, 0, 255], [255, 0, 170], [255, 0, 85]]

NUM_KPTS = 17

CTX = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

def draw_pose(keypoints,img):
    """draw the keypoints and the skeletons.
    :params keypoints: the shape should be equal to [17,2]
    :params img:
    """
    assert keypoints.shape == (NUM_KPTS,2)
    for i in range(len(SKELETON)):
        kpt_a, kpt_b = SKELETON[i][0], SKELETON[i][1]
        x_a, y_a = keypoints[kpt_a][0],keypoints[kpt_a][1]
        x_b, y_b = keypoints[kpt_b][0],keypoints[kpt_b][1] 
        cv2.circle(img, (int(x_a), int(y_a)), 6, CocoColors[i], -1)
        cv2.circle(img, (int(x_b), int(y_b)), 6, CocoColors[i], -1)
        cv2.line(img, (int(x_a), int(y_a)), (int(x_b), int(y_b)), CocoColors[i], 2)

def draw_bbox(box,img):
    """draw the detected bounding box on the image.
    :param img:
    """
    cv2.rectangle(img, box[0], box[1], color=(0, 255, 0),thickness=3)


def get_id_num(tracked_boxes):
    """
    Get the SORT tracker ID number of the bounding box with the biggest area 
    """
    max_area = 0
    id_num = 0
    for box in tracked_boxes:
        box_area = (box[2] - box[0]) * (box[3] - box[1])
        if box_area > max_area:
            max_area = box_area
            id_num = box[4]
    
    return id_num


def get_person_detection_boxes(model, img, tracker, id_num, threshold=0.5):
    pred = model(img)
    pred_classes = [COCO_INSTANCE_CATEGORY_NAMES[i]
                    for i in list(pred[0]['labels'].cpu().numpy())]  # Get the Prediction Score
    pred_boxes = [[(i[0], i[1]), (i[2], i[3])]
                  for i in list(pred[0]['boxes'].detach().cpu().numpy())]  # Bounding boxes
    pred_score = list(pred[0]['scores'].detach().cpu().numpy())
    if not pred_score or max(pred_score)<threshold:
        return [], id_num

    # Get list of index with score greater than threshold
    pred_t = [pred_score.index(x) for x in pred_score if x > threshold][-1]
    pred_boxes = pred_boxes[:pred_t+1]
    pred_classes = pred_classes[:pred_t+1]

    person_boxes = []
    for idx, box in enumerate(pred_boxes):
        if pred_classes[idx] == 'person':
            # Create array of structure [bb_x1, bb_y1, bb_x2, bb_y2, score] for use with SORT tracker
            box = [coord for pos in box for coord in pos]
            box.append(pred_score[idx])
            person_boxes.append(box)
    
    # Get ID's for each person
    person_boxes = np.array(person_boxes)
    boxes_tracked = tracker.update(person_boxes)  
    
    # If this is the first frame, get the ID of the bigger bounding box (person more in focus, most likely the thrower)  
    if id_num is None:
        id_num = get_id_num(boxes_tracked)

    # Turn into [[(x1, y2), (x2, y2)]]
    try:
        person_box = [box for box in boxes_tracked if box[4] == id_num][0]
        person_box = [[(person_box[0], person_box[1]), (person_box[2], person_box[3])]]
        return person_box, id_num

    # If detections weren't made for our thrower in a frame for some reason, return nothing to be smoothed later
    # As long as the thrower is detected within the next "max_age" frames, it will be assigned the same ID as before
    except IndexError:
        return [], id_num


def get_pose_estimation_prediction(pose_model, image, center, scale):
    rotation = 0

    # pose estimation transformation
    trans = get_affine_transform(center, scale, rotation, cfg.MODEL.IMAGE_SIZE)
    model_input = cv2.warpAffine(
        image,
        trans,
        (int(cfg.MODEL.IMAGE_SIZE[0]), int(cfg.MODEL.IMAGE_SIZE[1])),
        flags=cv2.INTER_LINEAR)
    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225]),
    ])

    # pose estimation inference
    model_input = transform(model_input).unsqueeze(0)
    # switch to evaluate mode
    pose_model.eval()
    with torch.no_grad():
        # compute output heatmap
        output = pose_model(model_input)
        preds, max_vals = get_final_preds(
            cfg,
            output.clone().cpu().numpy(),
            np.asarray([center]),
            np.asarray([scale]))

        return np.concatenate((preds, max_vals),2)


def box_to_center_scale(box, model_image_width, model_image_height):
    """convert a box to center,scale information required for pose transformation
    Parameters
    ----------
    box : list of tuple
        list of length 2 with two tuples of floats representing
        bottom left and top right corner of a box
    model_image_width : int
    model_image_height : int

    Returns
    -------
    (numpy array, numpy array)
        Two numpy arrays, coordinates for the center of the box and the scale of the box
    """
    center = np.zeros((2), dtype=np.float32)

    bottom_left_corner = box[0]
    top_right_corner = box[1]
    box_width = top_right_corner[0]-bottom_left_corner[0]
    box_height = top_right_corner[1]-bottom_left_corner[1]
    bottom_left_x = bottom_left_corner[0]
    bottom_left_y = bottom_left_corner[1]
    center[0] = bottom_left_x + box_width * 0.5
    center[1] = bottom_left_y + box_height * 0.5

    aspect_ratio = model_image_width * 1.0 / model_image_height
    pixel_std = 200

    if box_width > aspect_ratio * box_height:
        box_height = box_width * 1.0 / aspect_ratio
    elif box_width < aspect_ratio * box_height:
        box_width = box_height * aspect_ratio
    scale = np.array(
        [box_width * 1.0 / pixel_std, box_height * 1.0 / pixel_std],
        dtype=np.float32)
    if center[0] != -1:
        scale = scale * 1.25

    return center, scale


# def parse_args():
    
#     parser = argparse.ArgumentParser(description='Train keypoints network')
#     # general
#     parser.add_argument('--cfg', type=str, default=f'{cur_dir}/inference-config.yaml')
#     parser.add_argument('--video', type=str)
#     parser.add_argument('--write',action='store_true')
#     parser.add_argument('--showFps',action='store_true')
#     parser.add_argument('--output_dir',type=str, default='/')

#     parser.add_argument('opts',
#                         help='Modify config options using the command-line',
#                         default=None,
#                         nargs=argparse.REMAINDER)

#     args = parser.parse_args()

class Bunch:
    def __init__(self, **kwds):
        self.__dict__.update(kwds)

def get_deepHRnet_keypoints(video, output_dir=None, output_video=False, save_kpts=False, custom_model=None, max_age=3):

    keypoints = None
    # cudnn related setting
    cudnn.benchmark = cfg.CUDNN.BENCHMARK
    torch.backends.cudnn.deterministic = cfg.CUDNN.DETERMINISTIC
    torch.backends.cudnn.enabled = cfg.CUDNN.ENABLED

    #args = parses_args(video, output_dir)
    update_config(cfg, Bunch(cfg=f'{cur_dir}/inference-config.yaml', opts=None))

    box_model = torchvision.models.detection.fasterrcnn_resnet50_fpn(pretrained=True)
    box_model.to(CTX)
    box_model.eval()

    pose_model = eval('models.'+cfg.MODEL.NAME+'.get_pose_net')(
        cfg, is_train=False
    )

    model_to_use = cfg.TEST.MODEL_FILE
    if custom_model:
        model_to_use = custom_model

    print('=> loading model from {}'.format(model_to_use))
    if torch.cuda.is_available():
        pose_model.load_state_dict(torch.load(model_to_use), strict=False)
    else:
        pose_model.load_state_dict(torch.load(model_to_use, map_location='cpu'), strict=False)
            

    pose_model = torch.nn.DataParallel(pose_model, device_ids=cfg.GPUS)
    pose_model.to(CTX)
    pose_model.eval()

    # Loading an video or an video
    vidcap = cv2.VideoCapture(video)
    vid_name, vid_type = os.path.splitext(video)
    if output_dir:
        save_path = output_dir + f"/{vid_name}_deephrnet_output.{vid_type}"
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        vid_fps = vidcap.get(cv2.CAP_PROP_FPS)
        out = cv2.VideoWriter(save_path,fourcc, vid_fps, (int(vidcap.get(3)),int(vidcap.get(4))))

    # Initialize SORT Tracker
    tracker = Sort.Sort(max_age=max_age)
    id_num = None

    frame_num = 0
    while True:
        ret, image_bgr = vidcap.read()
        if ret:
            image = image_bgr[:, :, [2, 1, 0]]

            frame_num += 1
            print(f"Processing frame {frame_num}")

            input = []
            img = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
            img_tensor = torch.from_numpy(img/255.).permute(2,0,1).float().to(CTX)
            input.append(img_tensor)

            # object detection box
            pred_boxes, id_num = get_person_detection_boxes(box_model, input, tracker, id_num, threshold=0.95)

            # pose estimation
            if len(pred_boxes) >= 1:
                for box in pred_boxes:
                    center, scale = box_to_center_scale(box, cfg.MODEL.IMAGE_SIZE[0], cfg.MODEL.IMAGE_SIZE[1])
                    image_pose = image.copy() if cfg.DATASET.COLOR_RGB else image_bgr.copy()
                    pose_preds = get_pose_estimation_prediction(pose_model, image_pose, center, scale)  
                    if len(pose_preds)>=1:
                        for i, kpt in enumerate(pose_preds):
                            name = COCO_KEYPOINT_INDEXES[i]
                            if keypoints is None:
                                keypoints = np.array([kpt])
                            else:
                                keypoints = np.append(keypoints, [kpt], axis = 0)
                            #draw_pose(kpt,image_bgr) # draw the poses
                    else:
                        if keypoints is None:
                            keypoints = np.array([[[0, 0, 0]]*len(COCO_KEYPOINT_INDEXES)])
                        else:
                            keypoints = np.append(keypoints, [[[0, 0, 0]]*len(COCO_KEYPOINT_INDEXES)], axis=0)
            else:
                #Fill undetected frames with zero vectors
                if keypoints is None:
                    keypoints = np.array([[[0, 0, 0]]*len(COCO_KEYPOINT_INDEXES)])
                else:
                    keypoints = np.append(keypoints, [[[0, 0, 0]]*len(COCO_KEYPOINT_INDEXES)], axis=0)

            if output_video:
                out.write(image_bgr)

        else:
            print('Video ended')
            break
    if save_kpts:
        np.save(f"{output_dir}/keypoints", keypoints)
        print(f'keypoint saved to {output_dir}/keypoints.npy')

    cv2.destroyAllWindows()
    vidcap.release()
    if output_video:
        print('video has been saved as {}'.format(save_path))
        out.release()

    return keypoints
