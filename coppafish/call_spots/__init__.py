from .base import get_bled_codes, get_gene_efficiency, get_non_duplicate, compute_gene_efficiency
from .qual_check import omp_spot_score, quality_threshold, get_intensity_thresh
from .bleed_matrix import get_dye_channel_intensity_guess, compute_bleed_matrix
try:
    from .dot_product import compute_gene_scores
    from .qual_check import get_spot_intensity
except ImportError:
    from .qual_check import get_spot_intensity
