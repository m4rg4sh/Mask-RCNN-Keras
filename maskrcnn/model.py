import json

import tensorflow as tf
if tf.__version__ == '1.5.0':
    import keras
else:
    from tensorflow import keras

from .subgraphs.fpn_backbone_graph import BackboneGraph
from .subgraphs.rpn_graph import RPNGraph
from .subgraphs.proposal_layer import ProposalLayer
from .subgraphs.fpn_classifier_graph import FPNClassifierGraph
from .subgraphs.fpn_mask_graph import FPNMaskGraph
from .subgraphs.detection_layer import DetectionLayer

from .results import _results_from_tensor_values

class EnvironmentKeys(object):
  ESTIMATOR = 'tf.estimator'
  CORE_ML = 'coreml'

class Config(object):

    def __init__(self):
        self.architecture = 'resnet101'
        self.input_width = 1024
        self.input_height = 1024
        self.input_image_shape = (self.input_width, self.input_height, 3)
        self.num_classes = 1 + 80
        self.pre_nms_max_proposals = 6000
        self.max_proposals = 1000
        self.max_detections = 100
        self.pyramid_top_down_size = 256
        self.proposal_nms_threshold = 0.7
        self.detection_min_confidence = 0.7
        self.detection_nms_threshold = 0.3
        self.bounding_box_std_dev = [0.1, 0.1, 0.2, 0.2]
        self.classifier_pool_size = 7
        self.mask_pool_size = 14
        self.fc_layers_size = 1024
        self.anchor_scales = (32, 64, 128, 256, 512)
        self.anchor_ratios = [0.5, 1, 2]
        self.anchors_per_location = len(self.anchor_ratios)
        self.backbone_strides = [4, 8, 16, 32, 64]
        self.anchor_stride = 1

def _build_keras_models(config,environment):

    assert environment in [EnvironmentKeys.ESTIMATOR,
                           EnvironmentKeys.CORE_ML]

    input_image = keras.layers.Input(shape=config.input_image_shape, name="input_image")

    if environment == EnvironmentKeys.ESTIMATOR:
        input_id = keras.layers.Input(shape=[1], name="input_id", dtype=tf.string)
        input_bounding_box = keras.layers.Input(shape=[4], name="input_image_bounding_box", dtype=tf.float32)
        input_original_shape = keras.layers.Input(shape=[3], name="input_original_shape", dtype=tf.int32)


    backbone = BackboneGraph(input_tensor=input_image,
                             architecture=config.architecture,
                             pyramid_size=config.pyramid_top_down_size)

    P2, P3, P4, P5, P6 = backbone.build(environment=environment)

    rpn = RPNGraph(anchor_stride=config.anchor_stride,
                   anchors_per_location=config.anchors_per_location,
                   depth=config.pyramid_top_down_size,
                   feature_maps=[P2, P3, P4, P5, P6])

    # anchor_object_probs: Probability of each anchor containing only background or objects
    # anchor_deltas: Bounding box refinements to apply to each anchor to better enclose its object
    anchor_object_probs, anchor_deltas = rpn.build(environment=environment)

    # rois: Regions of interest (regions of the image that probably contain an object)
    proposal_layer = ProposalLayer(name="ROI",
                                   image_shape=config.input_image_shape[0:2],
                                   max_proposals=config.max_proposals,
                                   pre_nms_max_proposals=config.pre_nms_max_proposals,
                                   bounding_box_std_dev=config.bounding_box_std_dev,
                                   nms_threshold=config.proposal_nms_threshold,
                                   anchor_scales=config.anchor_scales,
                                   anchor_ratios=config.anchor_ratios,
                                   backbone_strides=config.backbone_strides,
                                   anchor_stride=config.anchor_stride)

    rois = proposal_layer([anchor_object_probs, anchor_deltas])

    mrcnn_feature_maps = [P2, P3, P4, P5]

    fpn_classifier_graph = FPNClassifierGraph(rois=rois,
                                              feature_maps=mrcnn_feature_maps,
                                              pool_size=config.classifier_pool_size,
                                              image_shape=config.input_image_shape,
                                              num_classes=config.num_classes,
                                              max_regions=config.max_proposals,
                                              fc_layers_size=config.fc_layers_size,
                                              pyramid_top_down_size=config.pyramid_top_down_size)

    # rois_class_probs: Probability of each class being contained within the roi
    # classifications: Bounding box refinements to apply to each roi to better enclose its object
    fpn_classifier_model, classifications = fpn_classifier_graph.build(environment=environment)

    detection_inputs = [rois, classifications]

    if environment == EnvironmentKeys.ESTIMATOR:
        detection_inputs.append(input_bounding_box)

    detections = DetectionLayer(name="detections",
                                max_detections=config.max_detections,
                                bounding_box_std_dev=config.bounding_box_std_dev,
                                detection_min_confidence=config.detection_min_confidence,
                                detection_nms_threshold=config.detection_nms_threshold,
                                image_shape=config.input_image_shape)(detection_inputs)

    if environment == EnvironmentKeys.CORE_ML:
        #TODO: eventually remove this useless operation, but now required for CoreML
        detections = keras.layers.Reshape((config.max_detections, 6))(detections)

    fpn_mask_graph = FPNMaskGraph(rois=detections,
                                  feature_maps=mrcnn_feature_maps,
                                  pool_size=config.mask_pool_size,
                                  image_shape=config.input_image_shape[0:2],
                                  num_classes=config.num_classes,
                                  max_regions=config.max_detections,
                                  pyramid_top_down_size=config.pyramid_top_down_size)

    fpn_mask_model, masks = fpn_mask_graph.build(environment=environment)

    inputs = [input_image]
    outputs = []

    if environment == EnvironmentKeys.ESTIMATOR:
        inputs.extend([input_id, input_bounding_box,input_original_shape])
        outputs.extend([input_id, input_bounding_box,input_original_shape])

    outputs.extend([detections,masks])

    mask_rcnn_model = keras.models.Model(inputs,
                                         outputs,
                                         name='mask_rcnn_model')

    return mask_rcnn_model, fpn_classifier_model, fpn_mask_model, proposal_layer.anchors

class MaskRCNNModel():

    _estimator = None

    def __init__(self,
                 config_path,
                 model_dir=None,
                 run_config=None,
                 initial_keras_weights=None):
        self.config = Config()
        with open(config_path) as file:
            config_dict = json.load(file)
            self.config.__dict__.update(config_dict)
        self.model_dir = model_dir
        self.run_config = run_config
        self.initial_keras_weights = initial_keras_weights

    def train(self,
              input_fn,
              steps=None,
              max_steps=None):
        estimator = self._get_estimator()
        return estimator.train(input_fn, steps=steps, max_steps=max_steps)

    def evaluate(self,
                 input_fn,
                 steps=None):
        estimator = self._get_estimator()
        metrics = estimator.evaluate(input_fn, steps=steps)
        return metrics

    def train_and_evaluate(self,
                           train_input_fn,
                           eval_input_fn,
                           train_steps=None,
                           max_train_steps=None,
                           eval_steps=None):
        self.train(train_input_fn, steps=train_steps, max_steps=max_train_steps)
        return self.evaluate(eval_input_fn, steps=eval_steps)

    def predict(self,
                dataset_id,
                input_fn,
                class_label_fn):
        estimator = self._get_estimator()
        tensor_values = estimator.predict(input_fn)
        return _results_from_tensor_values(tensor_values,
                                           dataset_id=dataset_id,
                                           class_label_fn=class_label_fn)

    def get_trained_keras_models(self):

        mask_rcnn_model, \
        fpn_classifier_model, \
        fpn_mask_model, \
        anchors = self._build_keras_models(environment="coreml")

        checkpoint = self._get_checkpoint()
        if checkpoint:
            #TODO: convert to keras weights
            #TODO: assign the weights to all relevant layers
            pass
        else:
            #Otherwise we load the weights
            assert self.initial_keras_weights is not None

            mask_rcnn_model.load_weights(self.initial_keras_weights, by_name=True)
            fpn_classifier_model.load_weights(self.initial_keras_weights, by_name=True)
            fpn_mask_model.load_weights(self.initial_keras_weights, by_name=True)

        return mask_rcnn_model, fpn_classifier_model, fpn_mask_model, anchors

    def export_estimator(self):
        # TODO:
        pass

    def _get_estimator(self):
        if self._estimator is None:
            self._estimator = self._build_estimator()
        return self._estimator

    def _get_checkpoint(self):
        #TODO: get the checkpoint from model_dir
        return None

    def _build_keras_models(self, environment):
        return _build_keras_models(self.config, environment)

    def _build_estimator(self):
        #TODO: we might want to skip this and load the model from the model_dir?
        mask_rcnn_model, _, _, _ = self._build_keras_models(environment = "tf.estimator")

        #TODO: only load the weights if we do not have a checkpoint?
        if self.initial_keras_weights:
            mask_rcnn_model.load_weights(self.initial_keras_weights, by_name=True)

        optimizer = keras.optimizers.SGD(
            lr=0.001, momentum=0.9,
            clipnorm=5.0)

        optimizer = _CustomOptimizer()

        def custom_loss(y_true, y_pred):
            loss = keras.backend.constant(0)
            loss = keras.backend.stop_gradient(loss)
            return loss

        def mAP(y_true, y_pred):
            #TODO: perform mAP
            return keras.backend.constant(0)

        mask_rcnn_model.compile(
            optimizer=optimizer,
            loss=[custom_loss for _ in range(len(mask_rcnn_model.outputs))],
            metrics=[mAP])
        return model_to_estimator(mask_rcnn_model,
                                  model_dir=self.model_dir)

    #TEMPORARY
class _CustomOptimizer(keras.optimizers.Optimizer):
    def get_updates(self, loss, params):
        return []

if tf.__version__ != '1.5.0':
    from tensorflow.python.estimator import estimator as estimator_lib
    from tensorflow.python.estimator import keras as estimator_keras_lib


    def model_to_estimator(keras_model=None,
                           model_dir=None,
                           config=None):
        config = estimator_lib.maybe_overwrite_model_dir_and_session_config(config, model_dir)
        keras_model_fn = estimator_keras_lib._create_keras_model_fn(keras_model, None)
        warm_start_path = None
        if keras_model._is_graph_network:
            warm_start_path = estimator_keras_lib._save_first_checkpoint(keras_model, None, config)
        weight_names = [weight.name[:-2] for layer in keras_model.layers for weight in layer.weights]
        ws = estimator_lib.WarmStartSettings(ckpt_to_initialize_from=warm_start_path,
                                             vars_to_warm_start=weight_names)
        estimator = estimator_lib.Estimator(keras_model_fn,
                                            config=config,
                                            warm_start_from=ws)
        return estimator