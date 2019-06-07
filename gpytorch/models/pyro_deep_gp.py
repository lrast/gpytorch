import torch
import pyro
from .abstract_variational_gp import AbstractVariationalGP


class QuadratureDist(pyro.distributions.Distribution):
    def __init__(self, likelihood, function_dist):
        self.likelihood = likelihood
        self.function_dist = function_dist

    def log_prob(self, target):
        return self.likelihood.expected_log_prob(target, self.function_dist)

    def sample(self, sample_shape=torch.Size()):
        pass


class AbstractPyroHiddenGPLayer(AbstractVariationalGP):
    def __init__(self, variational_strategy, input_dims, output_dims, name_prefix=""):
        super().__init__(variational_strategy)
        self.input_dims = input_dims
        self.output_dims = output_dims
        self.output_dim_plate = pyro.plate(name_prefix + ".n_output_plate", self.output_dims)

    @property
    def variational_distribution(self):
        return self.variational_strategy.variational_distribution.variational_distribution

    def guide(self):
        with self.output_dim_plate:
            q_u_samples = pyro.sample(name_prefix + ".inducing_values", self.variational_distribution)
        return q_u_samples

    def model(self, inputs, return_samples=True):
        # Note: assumes inducing_points are not shared (e.g., are O_i x m x O_{i-1})
        # If shared, we need to expand and repeat m x d -> O_i x m x O_{i-1}
        inducing_points = self.variational_strategy.inducing_points
        num_induc = inducing_points.size(-2)
        minibatch_size = inputs.size(-2)

        # inputs (x) are either n x O_{0} or p x n x O_{i-1}
        inputs = inputs.contiguous()
        if inputs.dim() == 2:  # n x O_{0}, make O_{i} x n x O_{0}
            # Assume new input entirely
            inputs = inputs.unsqueeze(0)
            inputs = inputs.expand(self.output_dims, inputs.size(-2), self.input_dims)
        elif inputs.dim() == 3: # p x n x O_{i-1} -> O_{i} x p x n x O_{i-1}
            # Assume batch dim is samples, not output_dim
            inputs = inputs.unsqueeze(0)
            inputs = inputs.expand(self.output_dims, inputs.size(1), inputs.size(-2), self.input_dims)

        if inputs.dim() == 4:  # Convert O_{i} x p x n x O_{i-1} -> O_{i} x p*n x O_{i-1}
            num_samples = inputs.size(-3)
            inputs = inputs.view(self.output_dims, inputs.size(-2) * inputs.size(-3), self.input_dims)
            reshape_output = True
        else:
            reshape_output = False
            num_samples = None

        # Goal: return a p x n x O_{i} tensor.

        full_inputs = torch.cat([inducing_points, inputs], dim=-2)
        full_output = self.forward(full_inputs)
        full_mean, full_covar = full_output.mean, full_output.lazy_covariance_matrix

        # full mean is now O_{i} x (p*n + m)

        # Mean terms
        induc_mean = full_mean[..., :num_induc]  # O_{i} x m
        test_mean = full_mean[..., num_induc:]  # O_{i} x (p*n)

        # Covariance terms
        induc_induc_covar = full_covar[..., :num_induc, :num_induc].add_jitter()
        from ..lazy import CholLazyTensor, DiagLazyTensor
        induc_induc_covar = CholLazyTensor(induc_induc_covar.cholesky())

        # induc_induc_covar is K_mm and is O_{i} x m x m

        induc_data_covar = full_covar[..., :num_induc, num_induc:].evaluate() # O_{i} x m x p*n
        data_data_covar = full_covar[..., num_induc:, num_induc:]  # O_{i} x p*n x p*n

        # prior_distribution is p(u)
        # induc_mean is O_{i} x m
        # induc_induc_covar is O_{i} x m x m
        prior_distribution = full_output.__class__(induc_mean, induc_induc_covar)
        with self.output_dim_plate:
            p_u_samples = pyro.sample(self.name_prefix + ".inducing_values", prior_distribution)
        # p_u_samples is p x O_{i} x m

        solve_result = induc_induc_covar.inv_matmul(p_u_samples.unsqueeze(-1)).squeeze(-1)
        # solve_result is K_uu^{-1}u and is p x O_{i} x m

        # We need to multiply K_ux^{T} by solve_result.
        # K_ux^{T} is either O_{i} x m x n if this is the first layer in the deep GP
        # or it is O_{i} x m x p*n if it is any later layer.


        if num_samples is not None:  # This means we are in a later layer, and K_ux^{T} is O_{i} x m x p*n
            # We need to reshape O_{i} x m x p*n to p x O_{i} x n x m

            # Step 1: Uncoalesce the p*n dimension to be p x n
            induc_data_covar = induc_data_covar.view(
                self.output_dims,
                num_induc,
                num_samples,
                minibatch_size,
            )  # induc_data_covar is now O_{i} x m x p x n
            induc_data_covar = induc_data_covar.permute(2, 0, 3, 1)
            # induc_data_covar is now p x O_{i} x n x m

            # K_xx is also a problem, because it is O_{i} x pn x pn
            # data_data_covar is O_{i} x pn x pn
            data_data_diag = data_data_covar.diag().view(
                self.output_dims, solve_result.size(0), minibatch_size
            )

            # diag is O_{i} x p x n
            data_data_diag = data_data_diag.transpose(-3, -2)
            # diag is p x O_{i} x n
        else:  # This is the first layer, and K_ux^{T} is O_{i} x m x n, so there is no p
            # Nothing needs to be done to induc_data_covar
            # And the diagonal of K_xx is just K_xx.diag() (e.g., no p to pull out).
            data_data_diag = data_data_covar.diag()

        # Mean is K_xuK_uu^{-1}u
        # solve_result is already K_uu^{-1}u
        # so multiply induc_data_covar (K_ux^{T})
        means = induc_data_covar.transpose(-2,-1).matmul(solve_result.unsqueeze(-1)).squeeze(-1)
        # means is now p x O_{i} x n

        # induc_induc_covar is O_{i} x m x m
        # induc_data_covar is p x O_{i} x n x m
        diag_correction = induc_induc_covar.inv_quad(induc_data_covar, reduce_inv_quad=False)

        # Computes diag(K_xx) - diag(K_xuK_uu^{-1}K_ux)
        variances = DiagLazyTensor(
            (data_data_diag - diag_correction).clamp_min(0)
        )

        # variances is a diagonal matrix that is p x O_{i} x n x n
        p_f_dist = full_output.__class__(means, variances)

        if return_samples:
            if num_samples is not None:
                samples = p_f_dist.rsample(torch.Size())
                # samples are p x O_{i} x n
                # The next layer expects p x n x O_{i}, so transpose
                samples = samples.transpose(-2, -1)
            else:
                samples = p_f_dist.rsample(torch.Size([p_u_samples.size(-3)]))
                # samples are p x O_{i} x n
                samples = samples.transpose(-2, -1)

            return samples
        else:
            return p_f_dist

    def __call__(self, inputs):
        """
        Some pyro replay nonsense I don't understand goes here.
        """

class AbstractPyroDeepGP(AbstractPyroHiddenGPLayer):
    def __init__(
        self,
        variational_strategy,
        input_dims,
        output_dims,
        total_num_data,
        name_prefix="",
        hidden_gp_layers,
    ):
        super().__init__(
            variational_strategy,
            input_dims,
            output_dims,
            name_prefix=name_prefix,
        )

        self.hidden_gp_layers = hidden_gp_layers  # A list of AbstractPyroHiddenGPLayers
        self.total_num_data = total_num_data

    def guide(self, inputs, outputs):
        for hidden_gp_layer in self.hidden_gp_layers:
            hidden_gp_layer.guide()

        super().guide()

    def model(self, inputs, outputs):
        # First call hidden GP layers
        for hidden_gp_layer in self.hidden_gp_layers:
            inputs = hidden_gp_layer.model(inputs)

        p_f_dist = super().model(inputs, return_samples=False)
        minibatch_size = inputs.size(-2)

        with pyro.plate(self.name_prefix + ".data_plate", minibatch_size, dim=-1):
            with pyro.poutine.scale(scale=float(self.total_num_data / minibatch_size)):
                out_dist = QuadratureDist(self.likelihood, f_samples)
                return pyro.sample(self.name_prefix + ".output_value", out_dist, obs=output)