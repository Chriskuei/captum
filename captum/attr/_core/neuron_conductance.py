#!/usr/bin/env python3
import torch
from .._utils.approximation_methods import approximation_parameters
from .._utils.attribution import NeuronAttribution
from .._utils.batching import _batched_operator
from .._utils.common import (
    _reshape_and_sum,
    _extend_index_list,
    _format_input_baseline,
    _format_additional_forward_args,
    validate_input,
    _format_attributions,
    _expand_additional_forward_args,
    _expand_target,
)
from .._utils.gradient import compute_layer_gradients_and_eval


class NeuronConductance(NeuronAttribution):
    def __init__(self, forward_func, layer, device_ids=None):
        r"""
        Args:

            forward_func (callable):  The forward function of the model or any
                          modification of it
            layer (torch.nn.Module): Layer for which neuron attributions are computed.
                          Attributions for a particular neuron in the output of
                          this layer are computed using the argument neuron_index
                          in the attribute method.
                          Currently, only layers with a single tensor output are
                          supported.
            device_ids (list(int)): Device ID list, necessary only if forward_func
                          applies a DataParallel model. This allows reconstruction of
                          intermediate outputs from batched results across devices.
                          If forward_func is given as the DataParallel model itself,
                          then it is not neccesary to provide this argument.
        """
        super().__init__(forward_func, layer, device_ids)

    def attribute(
        self,
        inputs,
        neuron_index,
        baselines=None,
        target=None,
        additional_forward_args=None,
        n_steps=50,
        method="riemann_trapezoid",
        internal_batch_size=None,
    ):
        r"""
            Computes conductance with respect to particular hidden neuron. The
            returned output is in the shape of the input, showing the attribution
            / conductance of each input feature to the selected hidden layer neuron.
            The details of the approach can be found here:
            https://arxiv.org/abs/1805.12233

            Args:

                inputs (tensor or tuple of tensors):  Input for which neuron
                            conductance is computed. If forward_func takes a single
                            tensor as input, a single input tensor should be provided.
                            If forward_func takes multiple tensors as input, a tuple
                            of the input tensors should be provided. It is assumed
                            that for all given input tensors, dimension 0 corresponds
                            to the number of examples, and if multiple input tensors
                            are provided, the examples must be aligned appropriately.
                neuron_index (int or tuple): Index of neuron in output of given
                              layer for which attribution is desired. Length of
                              this tuple must be one less than the number of
                              dimensions in the output of the given layer (since
                              dimension 0 corresponds to number of examples).
                              An integer may be provided instead of a tuple of
                              length 1.
                baselines (tensor or tuple of tensors, optional):  Baseline from which
                            integral is computed. If inputs is a single tensor,
                            baselines must also be a single tensor with exactly the same
                            dimensions as inputs. If inputs is a tuple of tensors,
                            baselines must also be a tuple of tensors, with matching
                            dimensions to inputs.
                            Default: zero tensor for each input tensor
                target (int, tuple, tensor or list, optional):  Output indices for
                            which gradients are computed (for classification cases,
                            this is usually the target class).
                            If the network returns a scalar value per example,
                            no target index is necessary.
                            For general 2D outputs, targets can be either:

                            - a single integer or a tensor containing a single
                                integer, which is applied to all input examples

                            - a list of integers or a 1D tensor, with length matching
                                the number of examples in inputs (dim 0). Each integer
                                is applied as the target for the corresponding example.

                            For outputs with > 2 dimensions, targets can be either:

                            - A single tuple, which contains #output_dims - 1
                                elements. This target index is applied to all examples.

                            - A list of tuples with length equal to the number of
                                examples in inputs (dim 0), and each tuple containing
                                #output_dims - 1 elements. Each tuple is applied as the
                                target for the corresponding example.

                            Default: None
                additional_forward_args (tuple, optional): If the forward function
                            requires additional arguments other than the inputs for
                            which attributions should not be computed, this argument
                            can be provided. It must be either a single additional
                            argument of a Tensor or arbitrary (non-tuple) type or a
                            tuple containing multiple additional arguments including
                            tensors or any arbitrary python types. These arguments
                            are provided to forward_func in order following the
                            arguments in inputs.
                            For a tensor, the first dimension of the tensor must
                            correspond to the number of examples. It will be
                            repeated for each of `n_steps` along the integrated
                            path. For all other types, the given argument is used
                            for all forward evaluations.
                            Note that attributions are not computed with respect
                            to these arguments.
                            Default: None
                n_steps (int, optional): The number of steps used by the approximation
                            method. Default: 50.
                method (string, optional): Method for approximating the integral,
                            one of `riemann_right`, `riemann_left`, `riemann_middle`,
                            `riemann_trapezoid` or `gausslegendre`.
                            Default: `gausslegendre` if no method is provided.
                internal_batch_size (int, optional): Divides total #steps * #examples
                            data points into chunks of size internal_batch_size,
                            which are computed (forward / backward passes)
                            sequentially.
                            For DataParallel models, each batch is split among the
                            available devices, so evaluations on each available
                            device contain internal_batch_size / num_devices examples.
                            If internal_batch_size is None, then all evaluations are
                            processed in one batch.
                            Default: None

            Returns:
                *tensor* or tuple of *tensors* of **attributions**:
                - **attributions** (*tensor* or tuple of *tensors*):
                            Conductance for
                            particular neuron with respect to each input feature.
                            Attributions will always be the same size as the provided
                            inputs, with each value providing the attribution of the
                            corresponding input index.
                            If a single tensor is provided as inputs, a single tensor is
                            returned. If a tuple is provided for inputs, a tuple of
                            corresponding sized tensors is returned.

            Examples::

                >>> # ImageClassifier takes a single input tensor of images Nx3x32x32,
                >>> # and returns an Nx10 tensor of class probabilities.
                >>> # It contains an attribute conv1, which is an instance of nn.conv2d,
                >>> # and the output of this layer has dimensions Nx12x32x32.
                >>> net = ImageClassifier()
                >>> neuron_cond = NeuronConductance(net, net.conv1)
                >>> input = torch.randn(2, 3, 32, 32, requires_grad=True)
                >>> # To compute neuron attribution, we need to provide the neuron
                >>> # index for which attribution is desired. Since the layer output
                >>> # is Nx12x32x32, we need a tuple in the form (0..11,0..31,0..31)
                >>> # which indexes a particular neuron in the layer output.
                >>> # Computes neuron conductance for neuron with
                >>> # index (4,1,2).
                >>> attribution = neuron_cond.attribute(input, (4,1,2))
        """
        is_inputs_tuple = isinstance(inputs, tuple)

        inputs, baselines = _format_input_baseline(inputs, baselines)
        validate_input(inputs, baselines, n_steps, method)

        num_examples = inputs[0].shape[0]
        total_batch = num_examples * n_steps

        # Retrieve scaling factors for specified approximation method
        step_sizes_func, alphas_func = approximation_parameters(method)
        step_sizes, alphas = step_sizes_func(n_steps), alphas_func(n_steps)

        # Compute scaled inputs from baseline to final input.
        scaled_features_tpl = tuple(
            torch.cat(
                [baseline + alpha * (input - baseline) for alpha in alphas], dim=0
            ).requires_grad_()
            for input, baseline in zip(inputs, baselines)
        )

        additional_forward_args = _format_additional_forward_args(
            additional_forward_args
        )
        # apply number of steps to additional forward args
        # currently, number of steps is applied only to additional forward arguemnts
        # that are nd-tensors. It is assumed that the first dimension is
        # the number of batches.
        # dim -> (#examples * #steps x additional_forward_args[0].shape[1:], ...)
        input_additional_args = (
            _expand_additional_forward_args(additional_forward_args, n_steps)
            if additional_forward_args is not None
            else None
        )
        expanded_target = _expand_target(target, n_steps)

        # Conductance Gradients - Returns gradient of output with respect to
        # hidden layer and hidden layer evaluated at each input.
        layer_gradients, layer_eval, input_grads = _batched_operator(
            compute_layer_gradients_and_eval,
            scaled_features_tpl,
            input_additional_args,
            internal_batch_size=internal_batch_size,
            forward_fn=self.forward_func,
            layer=self.layer,
            target_ind=expanded_target,
            gradient_neuron_index=neuron_index,
            device_ids=self.device_ids,
        )

        # Creates list of target neuron across batched examples (dimension 0)
        indices = _extend_index_list(total_batch, neuron_index)

        # Multiplies by appropriate gradient of output with respect to hidden neurons
        # mid_grads is a 1D Tensor of length num_steps*internal_batch_size,
        # containing mid layer gradient for each input step.
        mid_grads = torch.stack([layer_gradients[index] for index in indices])

        scaled_input_gradients = tuple(
            input_grad
            * mid_grads.reshape((total_batch,) + (1,) * (len(input_grad.shape) - 1))
            for input_grad in input_grads
        )

        # Mutliplies by appropriate step size.
        scaled_grads = tuple(
            scaled_input_gradient.contiguous().view(n_steps, -1)
            * torch.tensor(step_sizes).view(n_steps, 1).to(scaled_input_gradient.device)
            for scaled_input_gradient in scaled_input_gradients
        )

        # Aggregates across all steps for each tensor in the input tuple
        total_grads = tuple(
            _reshape_and_sum(scaled_grad, n_steps, num_examples, input_grad.shape[1:])
            for (scaled_grad, input_grad) in zip(scaled_grads, input_grads)
        )

        # computes attribution for each tensor in input tuple
        # attributions has the same dimentionality as inputs
        attributions = tuple(
            total_grad * (input - baseline)
            for total_grad, input, baseline in zip(total_grads, inputs, baselines)
        )
        return _format_attributions(is_inputs_tuple, attributions)
