from typing import List, Optional, Tuple, NamedTuple, Dict
from mlagents.torch_utils import torch, nn
import numpy as np

from mlagents.trainers.torch.encoders import (
    SimpleVisualEncoder,
    ResNetVisualEncoder,
    NatureVisualEncoder,
    SmallVisualEncoder,
    VectorInput,
)
from mlagents.trainers.settings import EncoderType, ScheduleType
from mlagents.trainers.exception import UnityTrainerException
from mlagents_envs.base_env import ActionSpec
from mlagents.trainers.torch.distributions import DistInstance, DiscreteDistInstance


class AgentAction(NamedTuple):
    continuous_tensor: torch.Tensor
    discrete_list: List[torch.Tensor]

    @property
    def discrete_tensor(self):
        return torch.stack(self.discrete_list, dim=-1)

    def to_numpy_dict(self) -> Dict[str, np.ndarray]:
        array_dict: Dict[str, np.ndarray] = {}
        if self.continuous_tensor is not None:
            array_dict["continuous_action"] = ModelUtils.to_numpy(
                self.continuous_tensor
            )
        if self.discrete_list is not None:
            array_dict["discrete_action"] = ModelUtils.to_numpy(
                self.discrete_tensor[:, 0, :]
            )
        return array_dict

    def to_tensor_list(self) -> List[torch.Tensor]:
        tensor_list: List[torch.Tensor] = []
        if self.continuous_tensor is not None:
            tensor_list.append(self.continuous_tensor)
        if self.discrete_list is not None:
            tensor_list += (
                self.discrete_list
            )  # Note this is different for ActionLogProbs
        return tensor_list

    @staticmethod
    def create(
        tensor_list: List[torch.Tensor], action_spec: ActionSpec
    ) -> "AgentAction":
        continuous: torch.Tensor = None
        discrete: List[torch.Tensor] = None
        _offset = 0
        if action_spec.continuous_size > 0:
            continuous = tensor_list[0]
            _offset = 1
        if action_spec.discrete_size > 0:
            discrete = tensor_list[_offset:]
        return AgentAction(continuous, discrete)

    @staticmethod
    def extract(buff: Dict[str, np.ndarray]) -> "AgentAction":
        continuous: torch.Tensor = None
        discrete: List[torch.Tensor] = None
        if "continuous_action" in buff:
            continuous = ModelUtils.list_to_tensor(buff["continuous_action"])
        if "discrete_action" in buff:
            discrete_tensor = ModelUtils.list_to_tensor(buff["discrete_action"])
            discrete = [
                discrete_tensor[..., i] for i in range(discrete_tensor.shape[-1])
            ]
        return AgentAction(continuous, discrete)


class ActionLogProbs(NamedTuple):
    continuous_tensor: torch.Tensor
    discrete_list: List[torch.Tensor]
    all_discrete_list: List[torch.Tensor]

    @property
    def discrete_tensor(self):
        return torch.stack(self.discrete_list, dim=-1)
        return torch.cat([_disc.unsqueeze(-1) for _disc in self.discrete_list], dim=1)

    @property
    def all_discrete_tensor(self):
        return torch.cat(self.all_discrete_list, dim=1)

    def to_numpy_dict(self) -> Dict[str, np.ndarray]:
        array_dict: Dict[str, np.ndarray] = {}
        if self.continuous_tensor is not None:
            array_dict["continuous_log_probs"] = ModelUtils.to_numpy(
                self.continuous_tensor
            )
        if self.discrete_list is not None:

            array_dict["discrete_log_probs"] = ModelUtils.to_numpy(self.discrete_tensor)
        return array_dict

    def to_tensor_list(self) -> List[torch.Tensor]:
        tensor_list: List[torch.Tensor] = []
        if self.continuous_tensor is not None:
            tensor_list.append(self.continuous_tensor)
        if self.discrete_list is not None:
            tensor_list.append(
                self.discrete_tensor
            )  # Note this is different for AgentActions
        return tensor_list

    def flatten(self) -> torch.Tensor:
        return torch.cat(self.to_tensor_list(), dim=1)

    @staticmethod
    def create(
        log_prob_list: List[torch.Tensor],
        action_spec: ActionSpec,
        all_log_prob_list: List[torch.Tensor] = None,
    ) -> "ActionLogProbs":
        continuous: torch.Tensor = None
        discrete: List[torch.Tensor] = None
        _offset = 0
        if action_spec.continuous_size > 0:
            continuous = log_prob_list[0]
            _offset = 1
        if action_spec.discrete_size > 0:
            discrete = log_prob_list[_offset:]
        return ActionLogProbs(continuous, discrete, all_log_prob_list)

    @staticmethod
    def extract(buff: Dict[str, np.ndarray]) -> "ActionLogProbs":
        continuous: torch.Tensor = None
        discrete: List[torch.Tensor] = None
        if "continuous_log_probs" in buff:
            continuous = ModelUtils.list_to_tensor(buff["continuous_log_probs"])
        if "discrete_log_probs" in buff:
            discrete_tensor = ModelUtils.list_to_tensor(buff["discrete_log_probs"])
            discrete = [
                discrete_tensor[..., i] for i in range(discrete_tensor.shape[-1])
            ]
        return ActionLogProbs(continuous, discrete, None)


class ModelUtils:
    # Minimum supported side for each encoder type. If refactoring an encoder, please
    # adjust these also.
    MIN_RESOLUTION_FOR_ENCODER = {
        EncoderType.MATCH3: 5,
        EncoderType.SIMPLE: 20,
        EncoderType.NATURE_CNN: 36,
        EncoderType.RESNET: 15,
    }

    class ActionFlattener:
        def __init__(self, action_spec: ActionSpec):
            self._specs = action_spec

        @property
        def flattened_size(self) -> int:
            if self._specs.is_continuous():
                return self._specs.continuous_size
            else:
                return sum(self._specs.discrete_branches)

        def forward(self, action: AgentAction) -> torch.Tensor:
            if self._specs.is_continuous():
                return action.continuous_tensor
            else:
                return torch.cat(
                    ModelUtils.actions_to_onehot(
                        torch.as_tensor(action.discrete_tensor, dtype=torch.long),
                        self._specs.discrete_branches,
                    ),
                    dim=1,
                )

    @staticmethod
    def update_learning_rate(optim: torch.optim.Optimizer, lr: float) -> None:
        """
        Apply a learning rate to a torch optimizer.
        :param optim: Optimizer
        :param lr: Learning rate
        """
        for param_group in optim.param_groups:
            param_group["lr"] = lr

    class DecayedValue:
        def __init__(
            self,
            schedule: ScheduleType,
            initial_value: float,
            min_value: float,
            max_step: int,
        ):
            """
            Object that represnets value of a parameter that should be decayed, assuming it is a function of
            global_step.
            :param schedule: Type of learning rate schedule.
            :param initial_value: Initial value before decay.
            :param min_value: Decay value to this value by max_step.
            :param max_step: The final step count where the return value should equal min_value.
            :param global_step: The current step count.
            :return: The value.
            """
            self.schedule = schedule
            self.initial_value = initial_value
            self.min_value = min_value
            self.max_step = max_step

        def get_value(self, global_step: int) -> float:
            """
            Get the value at a given global step.
            :param global_step: Step count.
            :returns: Decayed value at this global step.
            """
            if self.schedule == ScheduleType.CONSTANT:
                return self.initial_value
            elif self.schedule == ScheduleType.LINEAR:
                return ModelUtils.polynomial_decay(
                    self.initial_value, self.min_value, self.max_step, global_step
                )
            else:
                raise UnityTrainerException(f"The schedule {self.schedule} is invalid.")

    @staticmethod
    def polynomial_decay(
        initial_value: float,
        min_value: float,
        max_step: int,
        global_step: int,
        power: float = 1.0,
    ) -> float:
        """
        Get a decayed value based on a polynomial schedule, with respect to the current global step.
        :param initial_value: Initial value before decay.
        :param min_value: Decay value to this value by max_step.
        :param max_step: The final step count where the return value should equal min_value.
        :param global_step: The current step count.
        :param power: Power of polynomial decay. 1.0 (default) is a linear decay.
        :return: The current decayed value.
        """
        global_step = min(global_step, max_step)
        decayed_value = (initial_value - min_value) * (
            1 - float(global_step) / max_step
        ) ** (power) + min_value
        return decayed_value

    @staticmethod
    def get_encoder_for_type(encoder_type: EncoderType) -> nn.Module:
        ENCODER_FUNCTION_BY_TYPE = {
            EncoderType.SIMPLE: SimpleVisualEncoder,
            EncoderType.NATURE_CNN: NatureVisualEncoder,
            EncoderType.RESNET: ResNetVisualEncoder,
            EncoderType.MATCH3: SmallVisualEncoder,
        }
        return ENCODER_FUNCTION_BY_TYPE.get(encoder_type)

    @staticmethod
    def _check_resolution_for_encoder(
        height: int, width: int, vis_encoder_type: EncoderType
    ) -> None:
        min_res = ModelUtils.MIN_RESOLUTION_FOR_ENCODER[vis_encoder_type]
        if height < min_res or width < min_res:
            raise UnityTrainerException(
                f"Visual observation resolution ({width}x{height}) is too small for"
                f"the provided EncoderType ({vis_encoder_type.value}). The min dimension is {min_res}"
            )

    @staticmethod
    def create_input_processors(
        observation_shapes: List[Tuple[int, ...]],
        h_size: int,
        vis_encode_type: EncoderType,
        normalize: bool = False,
    ) -> Tuple[nn.ModuleList, nn.ModuleList, int]:
        """
        Creates visual and vector encoders, along with their normalizers.
        :param observation_shapes: List of Tuples that represent the action dimensions.
        :param action_size: Number of additional un-normalized inputs to each vector encoder. Used for
            conditioining network on other values (e.g. actions for a Q function)
        :param h_size: Number of hidden units per layer.
        :param vis_encode_type: Type of visual encoder to use.
        :param unnormalized_inputs: Vector inputs that should not be normalized, and added to the vector
            obs.
        :param normalize: Normalize all vector inputs.
        :return: Tuple of visual encoders and vector encoders each as a list.
        """
        visual_encoders: List[nn.Module] = []
        vector_encoders: List[nn.Module] = []

        visual_encoder_class = ModelUtils.get_encoder_for_type(vis_encode_type)
        vector_size = 0
        visual_output_size = 0
        for i, dimension in enumerate(observation_shapes):
            if len(dimension) == 3:
                ModelUtils._check_resolution_for_encoder(
                    dimension[0], dimension[1], vis_encode_type
                )
                visual_encoders.append(
                    visual_encoder_class(
                        dimension[0], dimension[1], dimension[2], h_size
                    )
                )
                visual_output_size += h_size
            elif len(dimension) == 1:
                vector_size += dimension[0]
            else:
                raise UnityTrainerException(
                    f"Unsupported shape of {dimension} for observation {i}"
                )
        if vector_size > 0:
            vector_encoders.append(VectorInput(vector_size, normalize))
        # Total output size for all inputs + CNNs
        total_processed_size = vector_size + visual_output_size
        return (
            nn.ModuleList(visual_encoders),
            nn.ModuleList(vector_encoders),
            total_processed_size,
        )

    @staticmethod
    def list_to_tensor(
        ndarray_list: List[np.ndarray], dtype: Optional[torch.dtype] = torch.float32
    ) -> torch.Tensor:
        """
        Converts a list of numpy arrays into a tensor. MUCH faster than
        calling as_tensor on the list directly.
        """
        return torch.as_tensor(np.asanyarray(ndarray_list), dtype=dtype)

    @staticmethod
    def to_numpy(tensor: torch.Tensor) -> np.ndarray:
        """
        Converts a Torch Tensor to a numpy array. If the Tensor is on the GPU, it will
        be brought to the CPU.
        """
        return tensor.detach().cpu().numpy()

    @staticmethod
    def break_into_branches(
        concatenated_logits: torch.Tensor, action_size: List[int]
    ) -> List[torch.Tensor]:
        """
        Takes a concatenated set of logits that represent multiple discrete action branches
        and breaks it up into one Tensor per branch.
        :param concatenated_logits: Tensor that represents the concatenated action branches
        :param action_size: List of ints containing the number of possible actions for each branch.
        :return: A List of Tensors containing one tensor per branch.
        """
        action_idx = [0] + list(np.cumsum(action_size))
        branched_logits = [
            concatenated_logits[:, action_idx[i] : action_idx[i + 1]]
            for i in range(len(action_size))
        ]
        return branched_logits

    @staticmethod
    def actions_to_onehot(
        discrete_actions: torch.Tensor, action_size: List[int]
    ) -> List[torch.Tensor]:
        """
        Takes a tensor of discrete actions and turns it into a List of onehot encoding for each
        action.
        :param discrete_actions: Actions in integer form.
        :param action_size: List of branch sizes. Should be of same size as discrete_actions'
        last dimension.
        :return: List of one-hot tensors, one representing each branch.
        """
        onehot_branches = [
            torch.nn.functional.one_hot(_act.T, action_size[i]).float()
            for i, _act in enumerate(discrete_actions.long().T)
        ]
        return onehot_branches

    @staticmethod
    def dynamic_partition(
        data: torch.Tensor, partitions: torch.Tensor, num_partitions: int
    ) -> List[torch.Tensor]:
        """
        Torch implementation of dynamic_partition :
        https://www.tensorflow.org/api_docs/python/tf/dynamic_partition
        Splits the data Tensor input into num_partitions Tensors according to the indices in
        partitions.
        :param data: The Tensor data that will be split into partitions.
        :param partitions: An indices tensor that determines in which partition each element
        of data will be in.
        :param num_partitions: The number of partitions to output. Corresponds to the
        maximum possible index in the partitions argument.
        :return: A list of Tensor partitions (Their indices correspond to their partition index).
        """
        res: List[torch.Tensor] = []
        for i in range(num_partitions):
            res += [data[(partitions == i).nonzero().squeeze(1)]]
        return res

    @staticmethod
    def get_probs_and_entropy(
        action_list: List[torch.Tensor], dists: List[DistInstance]
    ) -> Tuple[List[torch.Tensor], torch.Tensor, Optional[torch.Tensor]]:
        log_probs_list = []
        all_probs_list = []
        entropies_list = []
        for action, action_dist in zip(action_list, dists):
            log_prob = action_dist.log_prob(action)
            log_probs_list.append(log_prob)
            entropies_list.append(action_dist.entropy())
            if isinstance(action_dist, DiscreteDistInstance):
                all_probs_list.append(action_dist.all_log_prob())
        entropies = torch.stack(entropies_list, dim=-1)
        if not all_probs_list:
            entropies = entropies.squeeze(-1)
        return log_probs_list, entropies, all_probs_list

    @staticmethod
    def masked_mean(tensor: torch.Tensor, masks: torch.Tensor) -> torch.Tensor:
        """
        Returns the mean of the tensor but ignoring the values specified by masks.
        Used for masking out loss functions.
        :param tensor: Tensor which needs mean computation.
        :param masks: Boolean tensor of masks with same dimension as tensor.
        """
        return (tensor.T * masks).sum() / torch.clamp(
            (torch.ones_like(tensor.T) * masks).float().sum(), min=1.0
        )

    @staticmethod
    def soft_update(source: nn.Module, target: nn.Module, tau: float) -> None:
        """
        Performs an in-place polyak update of the target module based on the source,
        by a ratio of tau. Note that source and target modules must have the same
        parameters, where:
            target = tau * source + (1-tau) * target
        :param source: Source module whose parameters will be used.
        :param target: Target module whose parameters will be updated.
        :param tau: Percentage of source parameters to use in average. Setting tau to
            1 will copy the source parameters to the target.
        """
        with torch.no_grad():
            for source_param, target_param in zip(
                source.parameters(), target.parameters()
            ):
                target_param.data.mul_(1.0 - tau)
                torch.add(
                    target_param.data,
                    source_param.data,
                    alpha=tau,
                    out=target_param.data,
                )
