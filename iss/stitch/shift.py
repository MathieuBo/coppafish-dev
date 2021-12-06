import numpy as np
from sklearn.neighbors import NearestNeighbors
from scipy.stats import iqr
from ..utils.base import setdiff2d


def shift_score(distances, thresh):
    """
    computes a score to quantify how good a shift is based on the distances between the neighbours found.
    the value of this score is approximately the number of close neighbours found.

    :param distances: numpy array, distances between each pair of neighbours
    :param thresh: float, basically the distance in pixels below which neighbours are a good match.
                   expected to be about 2
    :return: float
    """
    return np.sum(np.exp(-distances**2 / (2*thresh**2)))


def extend_array(array, extend_scale, direction='both'):
    """
    extrapolates array using its mean spacing in the direction specified by extend_sz values.

    :param array: numpy float array, probably produced with np.arange
        (expected to be in ascending order with constant step).
    :param extend_scale: integer, by how many values to extend the array
    :param direction: 'below', 'above' or 'both'. default: 'both'
        'below': array extended below the min value
        'above': array extended above the max value
        'both': array extended in both directions (by extend_sz in each direction).
    :return: numpy array
    """
    if extend_scale == 0:
        ext_array = array
    else:
        step = np.mean(np.ediff1d(array))
        ext_below = np.arange(array.min() - extend_scale*step, array.min(), step)
        ext_above = np.arange(array.max() + step, array.max() + extend_scale*step + step / 2, step)
        if direction == 'below':
            ext_array = np.concatenate((ext_below, array))
        elif direction == 'above':
            ext_array = np.concatenate((array, ext_above))
        elif direction == 'both':
            ext_array = np.concatenate((ext_below, array, ext_above))
        else:
            raise ValueError(f"direction specified was {direction}, whereas it should be 'below', 'above' or 'both'")
    return ext_array


def refined_shifts(shifts, best_shift, refined_scale=0.5, extend_scale=2):
    """
    If shifts is an array with mean spacing step then this builds array
    that covers from best_shift - extend_scale * step to best_shift + extend_scale * step with a spacing of
    step*refined_scale.
    The new step, step*refined_scale, is forced to be an integer.
    If only one shift provided, doesn't do anything.

    :param shifts: numpy float array
    :param best_shift: value in shifts to build new shifts around
    :param refined_scale: float, optional. scaling to apply to find new shift. default: 0.5
    :param extend_scale: integer, optional. by how many steps to build new shifts. default: 2.
    :return:
    """
    if np.size(shifts) == 1:
        refined_shifts = shifts
    else:
        step = np.mean(np.ediff1d(shifts))
        refined_step = np.ceil(refined_scale * step).astype(int)
        refined_shifts = np.arange(best_shift - extend_scale * step,
                                   best_shift + extend_scale * step + refined_step/2, refined_step)
    return refined_shifts


def get_best_shift(yxz_base, yxz_transform, score_thresh, y_shifts, x_shifts, z_shifts, ignore_shifts=None):
    """
    Finds the shift from those given that is best applied to yx_base to match yx_transform.

    :param yxz_base: numpy float array [n_spots_base x 3], coordinates of spots on base image (yxz units must be same)
    :param yxz_transform: numpy float array [n_spots_transform x 3], coordinates of spots on transformed image
        (yxz units must be same)
    :param score_thresh: float, basically the distance below which neighbours are a good match.
        expected to be about 2
    :param y_shifts: numpy float array (probably made with np.arange).
        all possible shifts to test in y direction.
    :param x_shifts: numpy float array (probably made with np.arange).
        all possible shifts to test in x direction.
    :param z_shifts: numpy float array (probably made with np.arange), optional.
        all possible shifts to test in z direction.
    :param ignore_shifts: numpy float array [n_ignore x 3]
        contains yxz shifts to not search over.
        default: None, meaning use all permutations of y_shifts, x_shifts, z_shifts.
    :return:
        best_shift: numpy float array, [shift_y, shift_x, shift_z]. Best shift found.
        best_score: float, score of best shift.
        median_score: float, median of scores of all shifts.
        iqr_score: float, interquartile range of scores of all shifts.
    """
    all_shifts = np.array(np.meshgrid(y_shifts, x_shifts, z_shifts)).T.reshape(-1, 3)
    if ignore_shifts is not None:
        all_shifts = setdiff2d(all_shifts, ignore_shifts)
    nbrs = NearestNeighbors(n_neighbors=1).fit(yxz_transform)
    score = np.zeros(all_shifts.shape[0])
    for i in range(all_shifts.shape[0]):
        yx_shifted = yxz_base + all_shifts[i]
        distances, _ = nbrs.kneighbors(yx_shifted)
        score[i] = shift_score(distances, score_thresh)
    best_shift_ind = score.argmax()
    return all_shifts[best_shift_ind], score[best_shift_ind], np.median(score), iqr(score)


def compute_shift(yxz_base, yxz_transform, min_score, min_score_auto_param, shift_score_thresh,
                  y_shifts, x_shifts, z_shifts, y_widen=0, x_widen=0, z_widen=0, z_scale=1):
    """
    This finds the shift from those given that is best applied to yxz_base to match yxz_transform.
    If the score of this is below min_score, a widened search is performed.
    If the score is above min_score, a refined search is done about the best shift so as to find the absolute
    best shift, not the best shift among those given.

    :param yxz_base: numpy integer array [n_spots_base x 3], pixel coordinates of spots on base image
    :param yxz_transform: numpy integer array [n_spots_transform x 3], pixel coordinates of spots on transformed image
    :param min_score: float or None. If score of best shift is below this, will search among the widened shifts.
        if None, min_score will be set to median(scores) + min_score_auto_param * iqr(scores)
    :param min_score_auto_param: float, the parameter used to find min_score if min_score not given.
        expected to be about 5 (definitely more than 1).
    :param shift_score_thresh: float, basically the distance in pixels below which neighbours are a good match.
        expected to be about 2
    :param y_shifts: numpy integer array (probably made with np.arange).
        all possible shifts to test in y direction.
    :param x_shifts: numpy integer array (probably made with np.arange).
        all possible shifts to test in x direction.
    :param z_shifts: numpy integer array (probably made with np.arange), optional.
        all possible shifts to test in z direction.
    :param y_widen: integer, by how many shifts to extend search in y direction if score below min_score.
        (this many are added above and below current range).
        default: 0, if all _widen parameters are 0, widened search is never performed.
    :param x_widen: integer, by how many shifts to extend search in x direction if score below min_score.
        (this many are added above and below current range).
        default: 0, if all _widen parameters are 0, widened search is never performed.
    :param z_widen: integer, by how many shifts to extend search in z direction if score below min_score.
        (this many are added above and below current range).
        default: 0, if all _widen parameters are 0, widened search is never performed.
    :param z_scale: float, by what scale factor to multiply z coordinates to make them same units as xy.
        i.e. z_pixel_size / xy_pixel_size
        default: 1.
    :return:
        best_shift: numpy integer array, [shift_y, shift_x, shift_z]. Best shift found, (shift_z has not been scaled).
        best_score: float, score of best shift.
        min_score: same as input, unless input was None in which case this is the calculated value.
    """
    yxz_base[:, 2] = yxz_base[:, 2] * z_scale
    yxz_transform[:, 2] = yxz_transform[:, 2] * z_scale
    shift, score, score_median, score_iqr = get_best_shift(yxz_base, yxz_transform, shift_score_thresh,
                                                           y_shifts, x_shifts, z_shifts*z_scale)
    # save initial_shifts so don't look over same shifts twice
    initial_shifts = np.array(np.meshgrid(y_shifts, x_shifts, z_shifts*z_scale)).T.reshape(-1, 3)
    if min_score is None:
        min_score = score_median + min_score_auto_param * score_iqr
    if score < min_score and np.max([y_widen, x_widen, z_widen]) > 0:
        # look over extended range of shifts if score below threshold
        y_shifts = extend_array(y_shifts, y_widen)
        x_shifts = extend_array(x_shifts, x_widen)
        z_shifts = extend_array(z_shifts, z_widen)
        shift, score, score_median2, score_iqr2 = get_best_shift(yxz_base, yxz_transform, shift_score_thresh,
                                                                 y_shifts, x_shifts, z_shifts*z_scale, initial_shifts)
    if score > min_score:
        # refined search near maxima with half the step
        y_shifts = refined_shifts(y_shifts, shift[0])
        x_shifts = refined_shifts(x_shifts, shift[1])
        z_shifts = refined_shifts(z_shifts, shift[2]/z_scale)
        shift2, score2, _, _ = get_best_shift(yxz_base, yxz_transform, shift_score_thresh, y_shifts, x_shifts,
                                              z_shifts*z_scale, initial_shifts)
        if score2 > score:
            shift = shift2
        # final search with a step of 1
        y_shifts = refined_shifts(y_shifts, shift[0], refined_scale=1e-50, extend_scale=1)
        x_shifts = refined_shifts(x_shifts, shift[1], refined_scale=1e-50, extend_scale=1)
        z_shifts = refined_shifts(z_shifts, shift[2]/z_scale, refined_scale=1e-50, extend_scale=1)
        shift, score, _, _ = get_best_shift(yxz_base, yxz_transform, shift_score_thresh, y_shifts, x_shifts,
                                            z_shifts*z_scale, initial_shifts)
        shift[2] = shift[2] / z_scale
    return shift.astype(int), score, min_score
