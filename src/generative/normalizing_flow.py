import numpy as np
import tensorflow as tf

from variational import MultiLayerPerceptron


class PlanarFlow(object):
    """Class for defining operations for a normalizing flow."""

    def __init__(self, dim, w=None, b=None, u=None):
        """Sets up the parameters of a planar normalizing flow.

        Parameters:
        -----------
        w: tf.Tensor or None
            shape should be (N, dim)
        b: tf.Tensor or None
            shape should be (N)
        u: tf.Tensor or None
            shape should be (N, dim)
        """
        self.dim = dim
        if w is None or b is None or u is None:
            self.create_flow_variables()
        else:
            self.w = w
            self.b = b
            self.u = u
        # Enforcing reversibility.
        self.u_bar = self.reversible_constraint()
        # Total number of flows.
        self.n_flows = self.u.shape[0].value

    def get_flow_number(self):
        return self.n_flows

    def is_single_flow(self):
        """Indicates whether the flow consists of only one transformation."""
        return self.n_flows == 1

    def create_flow_variables(self):
        """Sets up variables for the single planar flow."""
        self.w = tf.Variable(np.random.normal(0, 1, [1, self.dim]))
        self.b = tf.Variable(np.random.normal(0, 1, 1))
        self.u = tf.Variable(np.random.normal(0, 1, [1, self.dim]))

    def reversible_constraint(self):
        dot = tf.reduce_sum((self.u * self.w), axis=1, keepdims=True)
        scalar = - 1 + tf.nn.softplus(dot) - dot
        norm_squared = tf.reduce_sum(self.w * self.w, axis=1, keepdims=True)
        comp = scalar * self.w / norm_squared
        return self.u + comp  

    def transform(self, inputs):
        """Transforms the inputs according to the state of the flow.
        
        Parameters:
        -----------
        inputs: tensorflow.Tensor
        Shape should be [self.n_flows, ?, self.dim] if self.n_flows > 1.
        If self.n_flows == 1 then shape should be [?, self.dim].
        """
        if self.is_single_flow():
            dialation = tf.matmul(inputs, self.w, transpose_b=True) + self.b
            return inputs + self.u_bar * tf.tanh(dialation)
        else:
            dialation = tf.matmul(inputs, tf.expand_dims(self.w, 2)) +\
            tf.expand_dims(tf.expand_dims(self.b, 1), 2)
            return inputs + tf.expand_dims(self.u_bar, 1) * tf.tanh(dialation)

    def log_det_jacobian(self, inputs):
        """Computes log-det-Jacobian for combination of inputs, flows.
        
        Parameters:
        -----------
        inputs: tensorflow.Tensor
        Shape should be [self.n_flows, ?, self.dim] if self.n_flows > 1.
        If self.n_flows == 1 then shape should be [?, self.dim].
        """
        if self.is_single_flow():
            dialation = tf.matmul(inputs, self.w, transpose_b=True) + self.b
            psi = 1.0 - tf.pow(tf.tanh(dialation), 2)
            det_jac = tf.matmul(self.u_bar, self.w, transpose_b=True) * psi
            return - tf.squeeze(tf.log(tf.abs(1 + det_jac)))
        else:
            dialation = tf.matmul(inputs, tf.expand_dims(self.w, 2)) +\
            tf.expand_dims(tf.expand_dims(self.b, 1), 2)
            psi = 1.0 - tf.pow(tf.tanh(dialation), 2)
            dot = tf.reduce_sum(self.u_bar * self.w, axis=1, keepdims=True)
            dot = tf.expand_dims(dot, 1)
            det_jac = dot * psi
            return - tf.squeeze(tf.log(tf.abs(1 + det_jac)))


class FlowRandomVariable(object):

    def __init__(self, dim, num_layers=1, flows=None,  base_dist=None):
        """Sets up a normalizing flow random variable.

        Parameters:
        -----------
        dim: int
            Dimensionality of the random variable.
        num_layers: int
            Number of layers of transformation for the nomralizing
            flow.
        flows: list of normalizing_flow.PlanarFlow
            Parameters of the reversible tranformations. If None,
            the parameters are set to tf.Variables that are initialized
            randomly.
        base_dist: tensorflow.distributions
            Probability distribution of the original space.
        """
        if base_dist is None:
            base_dist = tf.distributions.Normal(
                loc=np.zeros(dim), scale=np.ones(dim))
        self.dim = dim
        self.num_layers = num_layers
        self.base_dist = base_dist
        self.flows = flows
        if self.flows is None:
            self.num_layers = num_layers
            self.flows = []
            for i in range(self.num_layers):
                self.flows.append(PlanarFlow(dim))
        else:
            self.num_layrs = len(self.flows)

    def get_all_flow_params(self):
        """Returns parameters of the flow layers.

        Returns:
        --------
        tuple of size 3 where the first element is the concatenated
        w variables of all flows. Simliarly, the second and the third
        elements are concatenated u and b variables.
        """
        flow_range = range(self.num_layers)
        all_w = tf.concat([self.flows[i].w for i in flow_range], axis=1)
        all_u = tf.concat([self.flows[i].u for i in flow_range], axis=1)
        all_b = tf.concat([self.flows[i].b for i in flow_range], axis=0)
        return all_w, all_u, all_b

    def sample_log_prob(self, n_samples):
        """Provide samples from the flow distribution and its log prob."""
        if isinstance(self.base_dist, tf.distributions.Distribution):
            if self.flows[0].is_single_flow():
                samples = self.base_dist.sample(n_samples)
                log_prob = tf.reduce_sum(
                    self.base_dist.log_prob(samples), axis=1)
            else:
                n_flows = self.flows[0].get_flow_number()
                samples = self.base_dist.sample([n_flows, n_samples])
                log_prob = tf.reduce_sum(
                    self.base_dist.log_prob(samples), axis=2)           
        else:
            samples, log_prob = self.base_dist
        for flow in self.flows:
            log_prob += flow.log_det_jacobian(samples)
            samples = flow.transform(samples)
        return samples, log_prob

    def transform(self, x):
        for flow in self.flows:
            x = flow.transform(x)
        return x


class FlowConditionalVariable(object):
    """Class for a conditional random variable X|Y.

    This conditional density is a normalizing flow
    whose parameters are governed by an MLP of the
    variable that the density if conditioned upon.
    """

    def __init__(self, dim_x, y, flow_layers, hidden_units=[256, 128], base_dist=None):
        self.y = y
        self.dim_x = dim_x
        self.dim_y = y.shape[1].value
        self.flow_layers = flow_layers
        # Total number of inputs
        self.n_points = y.shape[0].value
        self.base_dist = base_dist
        self.hidden_units = hidden_units
        self.set_up_flows()

    def set_up_flows(self):
        # Non-linear function of Y
        w_mlp = MultiLayerPerceptron(
            self.y, layers=self.hidden_units + [self.dim_x * self.flow_layers], activation=tf.nn.tanh)
        all_w = w_mlp.get_output_layer()
        u_mlp = MultiLayerPerceptron(
            self.y, layers=self.hidden_units + [self.dim_x * self.flow_layers], activation=tf.nn.tanh)
        all_u = u_mlp.get_output_layer()
        b_mlp = MultiLayerPerceptron(
            self.y, layers=self.hidden_units + [self.flow_layers], activation=tf.nn.tanh)
        all_b = b_mlp.get_output_layer()
        # Slice the output layers into shapes of parameters of flows.
        flows = []
        for i in range(self.flow_layers):
            w = tf.slice(all_w, [0, i * self.dim_x], [-1, self.dim_x])
            u = tf.slice(all_u, [0, i * self.dim_x], [-1, self.dim_x])
            b = tf.squeeze(tf.slice(all_b, [0, i], [-1, 1]))
            flows.append(PlanarFlow(b, u=u, w=w, b=b))
        self.variable = FlowRandomVariable(
            dim=self.dim_x, flows=flows, base_dist=self.base_dist)
        # Set up object reference to internal parameters
        self.w = all_w
        self.u = all_u
        self.b = all_b

    def sample_log_prob(self, n_samples):
        return self.variable.sample_log_prob(n_samples)


class DynaFlowRandomVariable(object):

    def __init__(self, dim, time, num_layers, base_dist=None):
        """Sets up the prelimnary computation graphs."""
        # Full dimensionality of the latent space.
        self.full_dim = dim * time
        if base_dist is None:
            base_dist = tf.distributions.Normal(
                loc=np.zeros(self.full_dim), scale=np.ones(self.full_dim))
        self.dim = dim
        self.n_time = time
        self.num_layers = num_layers
        self.base_dist = base_dist
        self.flows = []
        # Set up planar flow layers.
        self.setup_flow_layers()

    def setup_flow_layers(self):
        for t in range(self.n_time - 1):
            self.flows.append([])
            for i in range(self.num_layers):
                self.flows[t].append(PlanarFlow(2 * self.dim))

    def sample_log_prob(self, n_samples):
        """Provide samples from the flow distribution and its log prob."""
        samples = self.base_dist.sample(n_samples)
        log_prob = tf.reduce_sum(
            self.base_dist.log_prob(samples), axis=1)
        # Transform two consecutive variables in time.
        final_samples = []
        single_time_latent_size = [n_samples, self.dim]
        pre_latent = tf.slice(samples, [0, 0], single_time_latent_size)
        for i, time_flow in enumerate(self.flows):
            cur_latent = tf.slice(
                samples, [0, (i + 1) * self.dim], single_time_latent_size)
            latent_pair = tf.concat([pre_latent, cur_latent], axis=1)
            for layer in time_flow:
                log_prob += layer.log_det_jacobian(latent_pair)
                latent_pair = layer.transform(latent_pair)
            # Accumulate the transformed time subsets.
            final_samples.append(
                tf.slice(latent_pair, [0, 0], single_time_latent_size))
            pre_latent = tf.slice(
                latent_pair, [0, self.dim], single_time_latent_size)
        # Last time stamp does does not have a following variable.
        final_samples.append(pre_latent)
        # Concatenate the subsets to form a single tensor.
        final_samples = tf.concat(final_samples, axis=1)
        return final_samples, log_prob


class DynaFlowConditionalRandomVariable(object):

    def __init__(self, y, dim, time, num_layers, base_dist=None):
        """Sets up the prelimnary computation graphs.

        Parameters:
        -----------y = tf.repeat(y, 2)
        y: numpy.ndarray0
            N input paths. Shape is (N, time * in_dim)
        dim: int
            Dimensionality of latent space.
        time: int
            Number of time step in the dynamical system.
        num_layers: int
            Number of normalizing flow layers. Regulates
            complexity of the model.
        base_dist: tf.distributions.Distribution or (tf.Tensor, tf.Tensor)
            Initial distribution to be transformed by the normalizing flow.
        """
        # Full dimensionality of the latent space.
        self.full_dim = dim * time
        if base_dist is None:
            base_dist = tf.distributions.Normal(
                loc=np.zeros(self.full_dim), scale=np.ones(self.full_dim))
        self.dim = dim
        self.n_time = time
        self.num_layers = num_layers
        self.base_dist = base_dist
        self.flows = []
        # Input properties
        self.y = y
        self.n_example = y.shape[0].value
        self.obs_dim = y.shape[1].value // self.n_time
        # Set up planar flow layers.
        self.setup_flow_layers()

    def unfold_time_pairs(self):
        unfold = tf.reshape(self.y, [self.n_example, self.n_time, self.obs_dim])
        x1 = tf.slice(unfold, [0, 0, 0], [-1, self.n_time - 1, -1])
        x2 = tf.slice(unfold, [0, 1, 0], [-1, -1, -1])
        unfold_x = tf.reshape(
            tf.concat([x1, x2], axis=2), [self.n_example * (self.n_time - 1), 2 * self.obs_dim])
        return unfold_x

    def get_flow_parameters(self, param_group, time, layer, dim):
        param_group = tf.reshape(
            param_group, [self.n_example, self.n_time - 1, dim * self.num_layers])
        return tf.squeeze(tf.slice(
            param_group,
            [0, time, layer * dim],
            [-1, 1, dim]))
    
    def setup_flow_layers(self):
        """Sets up the network regulating parameters of the flow."""
        hidden_units = 128
        unfolded = self.unfold_time_pairs()
        self.u_mlp = MultiLayerPerceptron(
            unfolded, layers=[hidden_units, hidden_units, self.dim * 2 * self.num_layers]).get_output_layer()
        self.w_mlp = MultiLayerPerceptron(
            unfolded, layers=[hidden_units, hidden_units, self.dim * 2 * self.num_layers]).get_output_layer()
        self.b_mlp = MultiLayerPerceptron(
            unfolded, layers=[hidden_units, hidden_units, self.num_layers]).get_output_layer()
        for t in range(self.n_time - 1):
            self.flows.append([])
            for i in range(self.num_layers):
                time_layer_u = self.get_flow_parameters(self.u_mlp, time=t, layer=i, dim=self.dim * 2)
                time_layer_w = self.get_flow_parameters(self.w_mlp, time=t, layer=i, dim=self.dim * 2)
                time_layer_b = self.get_flow_parameters(self.b_mlp, time=t, layer=i, dim=1)
                if len(time_layer_u.shape) == 1:
                    time_layer_u = tf.expand_dims(time_layer_u, 0)
                    time_layer_w = tf.expand_dims(time_layer_w, 0)
                    time_layer_b = tf.expand_dims(time_layer_b, 0)
                self.flows[t].append(
                    PlanarFlow(2 * self.dim, u=time_layer_u, w=time_layer_w, b=time_layer_b))

    def sample_log_prob(self, n_samples):
        """Provide samples from the flow distribution and its log prob."""
        if self.n_example == 1:
            samples = self.base_dist.sample(n_samples)
            log_prob = tf.reduce_sum(
                self.base_dist.log_prob(samples), axis=1)
            # Transform two consecutive variables in time.
            final_samples = []
            single_time_latent_size = [n_samples, self.dim]
            pre_latent = tf.slice(samples, [0, 0], single_time_latent_size)
            for i, time_flow in enumerate(self.flows):
                cur_latent = tf.slice(
                    samples, [0, (i + 1) * self.dim], single_time_latent_size)
                latent_pair = tf.concat([pre_latent, cur_latent], axis=1)
                for layer in time_flow:
                    log_prob += layer.log_det_jacobian(latent_pair)
                    latent_pair = layer.transform(latent_pair)
                # Accumulate the transformed time subsets.
                final_samples.append(
                    tf.slice(latent_pair, [0, 0], single_time_latent_size))
                pre_latent = tf.slice(
                    latent_pair, [0, self.dim], single_time_latent_size)
            # Last time stamp does does not have a following variable.
            final_samples.append(pre_latent)
            # Concatenate the subsets to form a single tensor.
            final_samples = tf.concat(final_samples, axis=1)
            return final_samples, log_prob
        else:
            samples = self.base_dist.sample([self.n_example, n_samples])
            log_prob = tf.reduce_sum(
                self.base_dist.log_prob(samples), axis=2)
            # Transform two consecutive variables in time.
            final_samples = []
            single_time_latent_size = [self.n_example, n_samples, self.dim]
            pre_latent = tf.slice(samples, [0, 0, 0], single_time_latent_size)
            for i, time_flow in enumerate(self.flows):
                cur_latent = tf.slice(
                    samples, [0, 0, (i + 1) * self.dim], single_time_latent_size)
                latent_pair = tf.concat([pre_latent, cur_latent], axis=2)
                for layer in time_flow:
                    log_prob += layer.log_det_jacobian(latent_pair)
                    latent_pair = layer.transform(latent_pair)
                # Accumulate the transformed time subsets.
                final_samples.append(
                    tf.slice(latent_pair, [0, 0, 0], single_time_latent_size))
                pre_latent = tf.slice(
                    latent_pair, [0, 0, self.dim], single_time_latent_size)
            # Last time stamp does does not have a following variable.
            final_samples.append(pre_latent)
            # Concatenate the subsets to form a single tensor.
            final_samples = tf.concat(final_samples, axis=2)
            return final_samples, log_prob
