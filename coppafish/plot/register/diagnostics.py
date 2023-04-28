import os
import numpy as np
import matplotlib.pyplot as plt
import napari
from qtpy.QtCore import Qt
from superqt import QDoubleRangeSlider, QDoubleSlider, QRangeSlider
from PyQt5.QtWidgets import QPushButton, QMainWindow, QSlider
from ...setup import Notebook, NotebookPage
from coppafish.register.preprocessing import change_basis, stack_images, create_shift_images, n_matches_to_frac_matches
from coppafish.register.base import huber_regression
from scipy.ndimage import affine_transform
plt.style.use('dark_background')


# there are 3 parts of the registration pipeline:
# 1. SVR
# 2. Cross tile outlier removal
# 3. ICP
# Above each of these viewers we will plot a number which shows which part it refers to

# 1 and 3
class RegistrationViewer:
    def __init__(self, nb: Notebook, t: int = None):
        """
        Function to overlay tile, round and channel with the anchor in napari and view the registration.
        This function is only long because we have to convert images to zyx and the transform to zyx * zyx
        Args:
            nb: Notebook
            t: common tile
        """
        # initialise frequently used variables, attaching those which are otherwise awkward to recalculate to self
        nbp_file, nbp_basic = nb.file_names, nb.basic_info
        use_rounds, use_channels = nbp_basic.use_rounds, nbp_basic.use_channels
        # set default transform to svr transform
        self.transform = nb.register.start_transform
        self.z_scale = nbp_basic.pixel_size_z / nbp_basic.pixel_size_xy
        self.r_ref, self.c_ref = nbp_basic.anchor_round, nb.basic_info.anchor_channel
        self.r_mid = len(use_rounds) // 2
        y_mid, x_mid, z_mid = nbp_basic.tile_centre
        self.new_origin = np.array([z_mid - 5, y_mid - 250, x_mid - 250])
        # Initialise file directories
        self.target_round_image = []
        self.target_channel_image = []
        self.base_image = None
        self.output_dir = os.path.join(nbp_file.output_dir, 'reg_images/')
        # Attach the 2 arguments to the object to be created and a new object for the viewer
        self.nb = nb
        self.viewer = napari.Viewer()
        # Make layer list invisible to remove clutter
        self.viewer.window.qt_viewer.dockLayerList.setVisible(False)
        self.viewer.window.qt_viewer.dockLayerControls.setVisible(False)

        # Now we will create 2 sliders. One will control all the contrast limits simultaneously, the other all anchor
        # images simultaneously.
        self.im_contrast_limits_slider = QRangeSlider(Qt.Orientation.Horizontal)
        self.anchor_contrast_limits_slider = QRangeSlider(Qt.Orientation.Horizontal)
        self.im_contrast_limits_slider.setRange(0, 256)
        self.anchor_contrast_limits_slider.setRange(0, 256)
        # Set default lower limit to 0 and upper limit to 100
        self.im_contrast_limits_slider.setValue(([0, 100]))
        self.anchor_contrast_limits_slider.setValue([0, 100])

        # Now we run a method that sets these contrast limits using napari
        # Create sliders!
        self.viewer.window.add_dock_widget(self.im_contrast_limits_slider, area="left", name='Imaging Contrast')
        self.viewer.window.add_dock_widget(self.anchor_contrast_limits_slider, area="left", name='Anchor Contrast')
        # Now create events that will recognise when someone has changed slider values
        self.anchor_contrast_limits_slider.valueChanged.connect(lambda x:
                                                                self.change_anchor_layer_contrast(x[0], x[1]))
        self.im_contrast_limits_slider.valueChanged.connect(lambda x: self.change_imaging_layer_contrast(x[0], x[1]))

        # Add buttons to change between registration methods
        self.method_buttons = ButtonMethodWindow('SVR')
        # I think this allows us to connect button status with the buttons in the viewer
        self.method_buttons.button_icp.clicked.connect(self.button_icp_clicked)
        self.method_buttons.button_svr.clicked.connect(self.button_svr_clicked)
        # Add these buttons as widgets in napari viewer
        self.viewer.window.add_dock_widget(self.method_buttons, area="left", name='Method')

        # Add buttons to show round regression
        # self.round_buttons = ButtonRoundWindow(use_rounds=nbp_basic.use_rounds)
        # We need to connect all these buttons to a single function which plots image in the same way
        # for r in use_rounds:
        #     self.round_buttons.__getattribute__(str(r)).clicked.connect(self.round_button_clicked(r))
        # Add these buttons as widgets in napari viewer
        # self.viewer.window.add_dock_widget(self.round_buttons, area="left", name='Round Regression')

        # Add buttons to select different tiles. Involves initialising variables use_tiles and tilepos
        tilepos_xy = np.roll(self.nb.basic_info.tilepos_yx, shift=1, axis=1)
        # Invert y as y goes downwards in the set geometry func
        num_rows = np.max(tilepos_xy[:, 1])
        tilepos_xy[:, 1] = num_rows - tilepos_xy[:, 1]
        # get use tiles
        use_tiles = self.nb.basic_info.use_tiles
        # If no tile provided then default to the first tile in use
        if t is None:
            t = use_tiles[0]
        # Store a copy of the working tile in the RegistrationViewer
        self.tile = t

        # Now create tile_buttons
        self.tile_buttons = ButtonTileWindow(tile_pos_xy=tilepos_xy, use_tiles=use_tiles, active_button=self.tile)
        for tile in use_tiles:
            # Now connect the button associated with tile t to a function that activates t and deactivates all else
            self.tile_buttons.__getattribute__(str(tile)).clicked.connect(self.create_tile_slot(tile))
        # Add these buttons as widgets in napari viewer
        self.viewer.window.add_dock_widget(self.tile_buttons, area="left", name='Tiles', add_vertical_stretch=False)

        # Create round_buttons
        self.round_buttons = ButtonRoundWindow(self.nb.basic_info.use_rounds)
        for rnd in use_rounds:
            # Now connect the button associated with tile t to a function that activates t and deactivates all else
            self.round_buttons.__getattribute__(str(rnd)).clicked.connect(self.create_round_slot(rnd))
        # Add these buttons as widgets in napari viewer
        self.viewer.window.add_dock_widget(self.round_buttons, area="left", name='Round Regression',
                                           add_vertical_stretch=False)

        # Create channel_buttons
        self.channel_buttons = ButtonChannelWindow(self.nb.basic_info.use_channels)
        for c in use_channels:
            # Now connect the button associated with tile t to a function that activates t and deactivates all else
            self.channel_buttons.__getattribute__(str(c)).clicked.connect(self.create_channel_slot(c))
        # Add these buttons as widgets in napari viewer
        self.viewer.window.add_dock_widget(self.channel_buttons, area="left", name='Channel Regression',
                                           add_vertical_stretch=False)

        # Get target images and anchor image
        self.get_images()

        # Plot images
        self.plot_images()

        napari.run()

    def change_anchor_layer_contrast(self, low, high):
        # Change contrast of anchor image (displayed in red), these are even index layers
        for i in range(0, 32, 2):
            self.viewer.layers[i].contrast_limits = [low, high]

    def change_imaging_layer_contrast(self, low, high):
        # Change contrast of anchor image (displayed in red), these are even index layers
        for i in range(1, 32, 2):
            self.viewer.layers[i].contrast_limits = [low, high]

    def button_svr_clicked(self):
        # Only allow one button pressed
        # Below does nothing if method is already svr and updates plot otherwise
        if self.method_buttons.method == 'SVR':
            self.method_buttons.button_svr.setChecked(True)
            self.method_buttons.button_icp.setChecked(False)
        else:
            self.method_buttons.button_svr.setChecked(True)
            self.method_buttons.button_icp.setChecked(False)
            self.method_buttons.method = 'SVR'
            # Because method has changed, also need to change transforms
            # Update set of transforms
            self.transform = self.nb.register.start_transform
            self.update_plot()

    def button_icp_clicked(self):
        # Only allow one button pressed
        # Below does nothing if method is already icp and updates plot otherwise
        if self.method_buttons.method == 'ICP':
            self.method_buttons.button_icp.setChecked(True)
            self.method_buttons.button_svr.setChecked(False)
        else:
            self.method_buttons.button_icp.setChecked(True)
            self.method_buttons.button_svr.setChecked(False)
            self.method_buttons.method = 'ICP'
            # Because method has changed, also need to change transforms
            # Update set of transforms
            self.transform = self.nb.register.transform
            self.update_plot()

    def create_round_slot(self, r):

        def round_button_clicked():
            use_rounds = self.nb.basic_info.use_rounds
            for rnd in use_rounds:
                self.round_buttons.__getattribute__(str(rnd)).setChecked(rnd == r)
            # We don't need to update the plot, we just need to call the viewing function
            view_regression_scatter(shift=self.nb.register.round_shift[self.tile, r],
                                    position=self.nb.register.round_position[self.tile, r],
                                    transform=self.nb.register.round_transform[self.tile, r])
        return round_button_clicked

    def create_tile_slot(self, t):

        def tile_button_clicked():
            # We're going to connect each button str(t) to a function that sets checked str(t) and nothing else
            # Also sets self.tile = t
            use_tiles = self.nb.basic_info.use_tiles
            for tile in use_tiles:
                self.tile_buttons.__getattribute__(str(tile)).setChecked(tile == t)
            self.tile = t
            self.update_plot()

        return tile_button_clicked

    def create_channel_slot(self, c):

        def channel_button_clicked():
            use_channels = self.nb.basic_info.use_channels
            for chan in use_channels:
                self.channel_buttons.__getattribute__(str(chan)).setChecked(chan == c)
            # We don't need to update the plot, we just need to call the viewing function
            view_regression_scatter(shift=self.nb.register.channel_shift[self.tile, c],
                                    position=self.nb.register.channel_position[self.tile, c],
                                    transform=self.nb.register.channel_transform[self.tile, c])
        return channel_button_clicked

    def update_plot(self):
        # Updates plot if tile or method has been changed
        # Update the images, we reload the anchor image even when it has not been changed, this should not be too slow
        self.clear_images()
        self.get_images()
        self.plot_images()

    def clear_images(self):
        # Function to clear all images currently in use
        n_images = len(self.viewer.layers)
        for i in range(n_images):
            del self.viewer.layers[0]

    def get_images(self):
        # reset initial target image lists to empty lists
        use_rounds, use_channels = self.nb.basic_info.use_rounds, self.nb.basic_info.use_channels
        self.target_round_image, self.target_channel_image = [], []
        t = self.tile
        # populate target arrays
        for r in use_rounds:
            file = 't'+str(t) + 'r'+str(r) + 'c'+str(self.c_ref)+'.npy'
            affine = change_basis(self.transform[t, r, self.c_ref], new_origin=self.new_origin, z_scale=self.z_scale)
            # Reset the spline interpolation order to 1 to speed things up
            self.target_round_image.append(affine_transform(np.load(os.path.join(self.output_dir, file)),
                                                            affine, order=1))

        for c in use_channels:
            file = 't' + str(t) + 'r' + str(self.r_mid) + 'c' + str(c) + '.npy'
            affine = change_basis(self.transform[t, self.r_mid, c], new_origin=self.new_origin, z_scale=self.z_scale)
            self.target_channel_image.append(affine_transform(np.load(os.path.join(self.output_dir, file)),
                                                              affine, order=1))
        # populate anchor image
        anchor_file = 't' + str(t) + 'r' + str(self.r_ref) + 'c' + str(self.c_ref) + '.npy'
        self.base_image = np.load(os.path.join(self.output_dir, anchor_file))

    def plot_images(self):
        use_rounds, use_channels = self.nb.basic_info.use_rounds, self.nb.basic_info.use_channels

        # We will add a point on top of each image and add features to it
        features = {'round': np.repeat(np.append(use_rounds, np.ones(len(use_channels)) * self.r_mid), 10).astype(int),
                    'channel': np.repeat(np.append(np.ones(len(use_rounds)) * self.c_ref, use_channels), 10).astype(
                        int)}

        # Define text
        text = {
            'string': 'Round {round} Channel {channel}',
            'size': 20,
            'color': 'Green'}
        text_anchor = {
            'string': 'Anchor: Round 7 Channel 18',
            'size': 20,
            'color': 'Red'}

        # Now go on to define point coords
        points = []
        points_anchor = []

        for r in use_rounds:
            self.viewer.add_image(self.base_image, blending='additive', colormap='red', translate=[0, 0, 1_000 * r],
                                  name='Anchor', contrast_limits=[0, 100])
            self.viewer.add_image(self.target_round_image[r], blending='additive', colormap='green',
                                  translate=[0, 0, 1_000 * r], name='Round ' + str(r) + ', Channel ' + str(self.c_ref),
                                  contrast_limits=[0, 100])
            # Add this to all z planes so still shows up when scrolling
            for z in range(10):
                points.append([z, -50, 250 + 1_000 * r])
                points_anchor.append([z, -100, 250 + 1_000 * r])

        for c in range(len(use_channels)):
            self.viewer.add_image(self.base_image, blending='additive', colormap='red', translate=[0, 1_000, 1_000 * c],
                                  name='Anchor', contrast_limits=[0, 100])
            self.viewer.add_image(self.target_channel_image[c], blending='additive', colormap='green',
                                  translate=[0, 1_000, 1_000 * c],
                                  name='Round ' + str(self.r_mid) + ', Channel ' + str(use_channels[c]),
                                  contrast_limits=[0, 100])
            for z in range(10):
                points.append([z, 950, 250 + 1_000 * c])
                points_anchor.append([z, 900, 250 + 1_000 * c])

        # Add text to image
        self.viewer.add_points(np.array(points), features=features, text=text, size=1)
        self.viewer.add_points(np.array(points_anchor), text=text_anchor, size=1)


class ButtonMethodWindow(QMainWindow):
    def __init__(self, active_button: str = 'SVR'):
        super().__init__()
        self.button_svr = QPushButton('SVR', self)
        self.button_svr.setCheckable(True)
        self.button_svr.setGeometry(75, 2, 50, 28)  # left, top, width, height

        self.button_icp = QPushButton('ICP', self)
        self.button_icp.setCheckable(True)
        self.button_icp.setGeometry(140, 2, 50, 28)  # left, top, width, height
        if active_button.lower() == 'icp':
            # Initially, show sub vol regression registration
            self.button_icp.setChecked(True)
            self.method = 'ICP'
        elif active_button.lower() == 'svr':
            self.button_svr.setChecked(True)
            self.method = 'SVR'
        else:
            raise ValueError(f"active_button should be 'SVR' or 'ICP' but {active_button} was given.")


class ButtonTileWindow(QMainWindow):
    def __init__(self, tile_pos_xy: np.ndarray, use_tiles: list, active_button: 0):
        super().__init__()
        # Loop through tiles, putting them in location as specified by tile pos xy
        for t in range(len(tile_pos_xy)):
            # Create a button for each tile
            button = QPushButton(str(t), self)
            # set the button to be checkable iff t in use_tiles
            button.setCheckable(t in use_tiles)
            button.setGeometry(tile_pos_xy[t, 0] * 70, tile_pos_xy[t, 1] * 40, 50, 28)
            # set active button as checked
            if active_button == t:
                button.setChecked(True)
                self.tile = t
            # Set button color = grey when hovering over
            # set colour of tiles in use to blue amd not in use to red
            if t in use_tiles:
                button.setStyleSheet("QPushButton"
                                     "{"
                                     "background-color : rgb(135, 206, 250);"
                                     "}"
                                     "QPushButton::hover"
                                     "{"
                                     "background-color : lightgrey;"
                                     "}"
                                     "QPushButton::pressed"
                                     "{"
                                     "background-color : white;"
                                     "}")
            else:
                button.setStyleSheet("QPushButton"
                                     "{"
                                     "background-color : rgb(240, 128, 128);"
                                     "}"
                                     "QPushButton::hover"
                                     "{"
                                     "background-color : lightgrey;"
                                     "}"
                                     "QPushButton::pressed"
                                     "{"
                                     "background-color : white;"
                                     "}")
            # Finally add this button as an attribute to self
            self.__setattr__(str(t), button)


class ButtonRoundWindow(QMainWindow):
    def __init__(self, use_rounds: list):
        super().__init__()
        # Loop through tiles, putting them in location as specified by tile pos xy
        for r in use_rounds:
            # Create a button for each tile
            button = QPushButton(str(r), self)
            # set the button to be checkable iff t in use_tiles
            button.setCheckable(True)
            button.setGeometry(r * 70, 40, 50, 28)
            # Set button color = grey when hovering over
            # set colour of tiles in use to blue amd not in use to red
            button.setStyleSheet("QPushButton"
                                 "{"
                                 "background-color : rgb(135, 206, 250);"
                                 "}"
                                 "QPushButton::hover"
                                 "{"
                                 "background-color : lightgrey;"
                                 "}"
                                 "QPushButton::pressed"
                                 "{"
                                 "background-color : white;"
                                 "}")
            # Finally add this button as an attribute to self
            self.__setattr__(str(r), button)
            self.round_regression = None


class ButtonChannelWindow(QMainWindow):
    def __init__(self, use_channels: list):
        super().__init__()
        # Loop through tiles, putting them in location as specified by tile pos xy
        for c in range(len(use_channels)):
            # Create a button for each tile
            button = QPushButton(str(use_channels[c]), self)
            # set the button to be checkable iff t in use_tiles
            button.setCheckable(True)
            button.setGeometry(c * 70, 40, 50, 28)
            # Set button color = grey when hovering over
            # set colour of tiles in use to blue amd not in use to red
            button.setStyleSheet("QPushButton"
                                 "{"
                                 "background-color : rgb(135, 206, 250);"
                                 "}"
                                 "QPushButton::hover"
                                 "{"
                                 "background-color : lightgrey;"
                                 "}"
                                 "QPushButton::pressed"
                                 "{"
                                 "background-color : white;"
                                 "}")
            # Finally add this button as an attribute to self
            self.__setattr__(str(use_channels[c]), button)


# 1
def view_regression_scatter(nb: Notebook, t: int, index: int, round: bool):
    """
    view 3 scatter plots for each data set shift vs positions
    Args:
        nb: Notebook
        t: tile
        index: round index if round, else channel index
        round: True if round, False if channel
    """
    # Transpose shift and position variables so coord is dimension 0, makes plotting easier
    if round:
        mode = 'Round'
        shift = nb.register_debug.round_shift[t, index].T
        subvol_transform = nb.register_debug.round_transform[t, index]
        icp_transform = nb.register.transform[t, index, nb.basic_info.anchor_channel]
    else:
        mode = 'Channel'
        shift = nb.register_debug.channel_shift[t, index].T
        subvol_transform = nb.register_debug.channel_transform[t, index]
        icp_transform = nb.register.transform[t, nb.basic_info.n_rounds // 2, index]
    position = nb.register_debug.position.T

    # Make ranges, wil be useful for plotting lines
    z_range = np.arange(np.min(position[0]), np.max(position[0]))
    yx_range = np.arange(np.min(position[1]), np.max(position[1]))
    coord_range = [z_range, yx_range, yx_range]
    # Need to add a central offset to all lines plotted
    tile_centre_zyx = np.roll(nb.basic_info.tile_centre, 1)
    central_offset_svr = np.zeros((3, 3))
    central_offset_icp = np.zeros((3, 3))
    for i in range(3):
        # This is a clever little trick to make j and k the dimensions that i is not
        j, k = (i + 1) % 3, (i + 2) % 3
        central_offset_svr[i] = subvol_transform[i, j] * tile_centre_zyx[j] + subvol_transform[i, k] * tile_centre_zyx[k]
        central_offset_icp[i] = icp_transform[i, j] * tile_centre_zyx[j] + icp_transform[i, k] * tile_centre_zyx[k]

    # Define the axes
    fig, axes = plt.subplots(3, 3)
    coord = ['Z', 'Y', 'X']
    # Now plot n_matches
    for i in range(3):
        for j in range(3):
            ax = axes[i, j]
            ax.scatter(position[i], shift[j], alpha=0.1)
            ax.plot(coord_range[i], (subvol_transform[i, j] - int(i == j)) * coord_range[i] +
                    subvol_transform[i, 3] + central_offset_svr[i, j], label='SVR')
            ax.plot(coord_range[i], (icp_transform[i, j] - int(i == j)) * coord_range[i] +
                    icp_transform[i, 3] + central_offset_icp[i, j], label='ICP')
            ax.set_ylabel(coord[j] + ' shift')
            ax.set_ylabel(coord[i] + ' position')
            ax.legend()
    # Add title
    plt.suptitle(mode + 'regression for Tile ' + str(t) + ', ' + mode + str(index))
    plt.show()


# 1
def view_pearson_hists(nb, t, num_bins=30):
    """
    function to view histogram of correlation coefficients for all subvol shifts of all round/channels.
    Args:
        nb: Notebook
        t: int tile under consideration
        num_bins: int number of bins in the histogram
    """
    nbp_basic, nbp_register_debug = nb.basic_info, nb.register_debug
    thresh = nb.get_config()['register']['r_thresh']
    round_corr, channel_corr = nbp_register_debug.round_corr[t], nbp_register_debug.channel_corr[t]
    n_rounds, n_channels_use = nbp_basic.n_rounds, len(nbp_basic.use_channels)
    use_channels = nbp_basic.use_channels
    cols = max(n_rounds, n_channels_use)

    for r in range(n_rounds):
        plt.subplot(2, cols, r + 1)
        counts, _ = np.histogram(round_corr[r], np.linspace(0, 1, num_bins))
        plt.hist(round_corr[r], bins=np.linspace(0, 1, num_bins))
        plt.vlines(x=thresh, ymin=0, ymax=np.max(counts), colors='r')
        plt.title('Similarity score for sub-volume shifts of tile ' + str(t) + ', round ' + str(r) +
                  '\n Success Ratio = ' + str(
            round(100 * sum(round_corr[r] > thresh) / round_corr.shape[1], 2)) + '%')

    for c in range(n_channels_use):
        plt.subplot(2, cols, cols + c + 1)
        counts, _ = np.histogram(channel_corr[use_channels[c]], np.linspace(0, 1, num_bins))
        plt.hist(channel_corr[use_channels[c]], bins=np.linspace(0, 1, num_bins))
        plt.vlines(x=thresh, ymin=0, ymax=np.max(counts), colors='r')
        plt.title('Similarity score for sub-volume shifts of tile ' + str(t) + ', channel ' + str(use_channels[c]) +
                  '\n Success Ratio = ' + str(
            round(100 * sum(channel_corr[use_channels[c]] > thresh) / channel_corr.shape[1], 2)) + '%')

    plt.suptitle('Similarity score distributions for all sub-volume shifts')
    plt.show()


# 1
def view_pearson_colourmap(nb, t):
    """
    function to view colourmap of correlation coefficients for all subvol shifts for all channels and rounds.

    Args:
        nb: Notebook
        t: int tile under consideration
    """
    # initialise frequently used variables
    nbp_basic, nbp_register_debug = nb.basic_info, nb.register_debug
    round_corr, channel_corr = nbp_register_debug.round_corr[t], nbp_register_debug.channel_corr[t]
    use_channels = nbp_basic.use_channels
    # Replace 0 with nans so they get plotted as black
    round_corr[round_corr == 0] = np.nan
    channel_corr[channel_corr == 0] = np.nan

    # plot round correlation and tile correlation
    fig, axes = plt.subplots(n_rows=2, n_cols=1)
    ax1, ax2 = axes[0, 0], axes[1, 0]
    # ax1 refers to round shifts
    im = ax1.imshow(round_corr, vmin=0, vmax=1, aspect='auto')
    ax1.xlabel('Sub-volume index')
    ax1.ylabel('Round')
    ax1.title('Round sub-volume shift scores')
    # ax2 refers to channel shifts
    ax2.subplot(1, 2, 2)
    im = ax2.imshow(channel_corr[:, use_channels], vmin=0, vmax=1)
    ax2.xlabel('Sub-volume index')
    ax2.ylabel('Channel')
    ax2.yticks(nbp_basic.use_channels)
    ax2.title('Channel sub-volume shift scores')

    # Add common colour bar
    fig.subplots_adjust(right=0.8)
    cbar_ax = fig.add_axes([0.85, 0.15, 0.05, 0.7])
    fig.colorbar(im, cax=cbar_ax)

    plt.suptitle('Similarity score distributions for all sub-volume shifts')


# 1
def view_pearson_colourmap_spatial(nb: Notebook, round: bool, t: int):
    """
    function to view colourmap of correlation coefficients along with spatial info for either all round shifts of a tile
    or all channel shifts of a tile.

    Args:
        nb: Notebook
        round: True if round, false if channel
        t: tile under consideration
    """

    # initialise frequently used variables
    config = nb.get_config()['register']
    if round:
        use = nb.basic_info.use_rounds
        corr = nb.register_debug.round_corr[t, use]
        mode = 'Round'
    else:
        use = nb.basic_info.use_channels
        corr = nb.register_debug.channel_corr[t, use]
        mode = 'Channel'

    # Set 0 correlations to nan, so they are plotted as black
    corr[corr == 0] = np.nan
    z_subvols, y_subvols, x_subvols = config['z_subvols'], config['y_subvols'], config['x_subvols']
    n_rc = corr.shape[0]

    fig, axes = plt.subplots(nrows=n_rc, ncols=z_subvols)
    # common axis labels
    fig.supxlabel('Z Sub-volume')
    fig.supylabel(mode)
    # Set row and column labels
    for ax, col in zip(axes[0], np.arange(z_subvols)):
        ax.set_title(col)
    for ax, row in zip(axes[:, 0], use):
        ax.set_ylabel(row, rotation=0, size='large')
    # Now plot each image
    for elem in range(n_rc):
        for z in range(z_subvols):
            ax = axes[elem, z]
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_x_label('X')
            ax.set_y_label('Y')
            im = ax.imshow(np.reshape(corr[z * y_subvols * x_subvols: (z + 1) * x_subvols * y_subvols],
                                      (y_subvols, x_subvols)), vmin=0, vmax=1)
    # add colour bar
    fig.subplots_adjust(right=0.8)
    cbar_ax = fig.add_axes([0.85, 0.15, 0.05, 0.7])
    fig.colorbar(im, cax=cbar_ax)

    plt.suptitle(mode + ' shift similarity scores for tile ' + str(t) + ' plotted spatially')


# 2
def shift_vector_field(nb: Notebook, round: bool):
    """
    Function to plot vector fields of predicted shifts vs shifts to see if we classify a shift as an outlier.
    Args:
        nb: Notebook
        round: True if round, False if Channel
    """
    nbp_basic, nbp_register_debug = nb.basic_info, nb.register_debug
    residual_thresh = nb.get_config()['register']['residual_threshold']
    use_tiles = nbp_basic.use_tiles
    tilepos_yx = nbp_basic.tilepos_yx[use_tiles]

    # Load in shift
    if round:
        mode = 'round'
        use_rc = nbp_basic.use_rounds
        shift = nbp_register_debug.round_transform_unregularised[use_tiles, :, :, 3]
    else:
        mode = 'channel'
        use_rc = nbp_basic.use_channels
        shift = nbp_register_debug.channel_transform_unregularised[use_tiles, :, :, 3]

    # record number of rounds/channels, tiles and initialise predicted shift
    n_t, n_rc = shift.shape[0], shift.shape[1]
    tilepos_yx_pad = np.vstack((tilepos_yx.T, np.ones(n_t))).T
    predicted_shift = np.zeros_like(shift)

    fig, axes = plt.subplots(nrows=3, ncols=n_rc)
    for elem in range(n_rc):
        # generate predicted shift for this r/c via huber regression
        transform = huber_regression(shift[elem], tilepos_yx)
        predicted_shift[:, elem] = tilepos_yx_pad @ transform.T

        # plot the predicted yx shift vs actual yx shift in row 0
        ax = axes[0, elem]
        ax.quiver(tilepos_yx[:, 1], tilepos_yx[:, 0], 10 * predicted_shift[:, elem, 2],
                  10 * predicted_shift[:, elem, 1], color='b', label='regularised')
        ax.quiver(tilepos_yx[:, 1], tilepos_yx[:, 0], 10 * shift[:, elem, 2], 10 * shift[:, elem, 1], color='r',
                  label='raw')
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_x_label('X')
        ax.set_y_label('Y')
        ax.set_title('XY shifts for ' + mode + ' ' + str(use_rc[elem]))

        # plot the predicted z shift vs actual z shift in row 1
        ax = axes[1, elem]
        ax.quiver(tilepos_yx[:, 1], tilepos_yx[:, 0], 0, 10 * predicted_shift[:, elem, 0], color='b')
        ax.quiver(tilepos_yx[:, 1], tilepos_yx[:, 0], 0, 10 * shift[:, elem, 2], color='r')
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_x_label('X')
        ax.set_y_label('Y')
        ax.set_title('Z shifts for ' + mode + ' ' + str(use_rc[elem]))

        # Plot image of norms of residuals at each tile in row 3
        ax = axes[2, elem]
        diff = make_residual_plot(residual=np.linalg.norm(predicted_shift[:, elem] - shift[:, elem]),
                                  nbp_basic=nbp_basic)
        outlier = np.argwhere(diff > residual_thresh)
        n_outliers = outlier.shape[0]
        im = ax.imshow(diff, vmin=0, vmax=10)
        # Now highlight in red the outlier pixels
        for pixel in range(n_outliers):
            rectangle = plt.Rectangle(outlier[pixel], 1, 1, fill='false', ec='r', linestyle=':', lw=4)
            ax.add_patch(rectangle)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_x_label('X')
        ax.set_y_label('Y')
        ax.set_title('Difference between prediction and observation for ' + mode + ' ' + str(use_rc[elem]))

    # Add global colour bar and legend
    lines_labels = [ax.get_legend_handles_labels() for ax in fig.axes]
    lines, labels = [sum(lol, []) for lol in zip(*lines_labels)]
    fig.legend(lines, labels)
    fig.subplots_adjust(right=0.8)
    cbar_ax = fig.add_axes([0.85, 0.15, 0.05, 0.7])
    fig.colorbar(im, cax=cbar_ax)


# 2
def zyx_shift_image(nb: Notebook, round: bool):
    """
        Function to plot overlaid images of predicted shifts vs shifts to see if we classify a shift as an outlier.
        Args:
            nb: Notebook
            round: Boolean indicating whether we are looking at round outlier removal, True if r, False if c
    """
    nbp_basic, nbp_register, nbp_register_debug = nb.basic_info, nb.register, nb.register_debug
    use_tiles = nbp_basic.use_tiles
    tilepos_yx = nbp_basic.tilepos_yx[use_tiles]

    # Load in shift
    if round:
        mode = 'Round'
        use = nbp_basic.use_rounds
        shift_raw = nbp_register_debug.round_transform_unregularised[use_tiles, :, :, 3]
        shift = nbp_register.round_transform[use_tiles, :, :, 3]
    else:
        mode = 'Channel'
        use = nbp_basic.use_channels
        shift_raw = nbp_register_debug.channel_transform_unregularised[use_tiles, :, :, 3]
        shift = nbp_register.channel_transform[use_tiles, :, :, 3]

    coord_label = ['Z', 'Y', 'X']
    n_t, n_rc = shift.shape[0], shift.shape[1]
    fig, axes = plt.subplots(nrows=3, ncols=n_rc)
    # common axis labels
    fig.supxlabel(mode)
    fig.supylabel('Coordinate (Z, Y, X)')

    # Set row and column labels
    for ax, col in zip(axes[0], coord_label):
        ax.set_title(col)
    for ax, row in zip(axes[:, 0], use):
        ax.set_ylabel(row, rotation=0, size='large')

    # Now plot each image
    for elem in range(n_rc):
        im_raw = create_shift_images(shift_raw[:, elem], tilepos_yx)
        im = create_shift_images(shift[:, elem], tilepos_yx)
        for coord in range(3):
            ax = axes[coord, elem]
            ax.set_xticks([])
            ax.set_yticks([])
            coord_im_stacked = stack_images(im_raw[coord], im[coord])
            ax.imshow(coord_im_stacked, vmin=np.min(shift_raw[:, :, :, coord]), vmax=np.max(shift_raw[:, :, :, coord]))
            ax.set_title(mode + ' ' + str(use[elem]) + ' ' + coord_label[coord] + ' shift for all tiles')

    fig.canvas.draw()
    plt.suptitle('Raw (top) vs regularised (bottom) shifts for all tiles. Row 1 = Z, Row 2 = Y, Row 3 = X.')
    plt.show()


# 2
def make_residual_plot(residual, nbp_basic):
    """
    generate image of residuals along with their tile positions
    Args:
        residual: n_tiles_use x 1 list of residuals
        nbp_basic: basic info notebook page
    """
    # Initialise frequently used variables
    use_tiles = nbp_basic.use_tiles
    tilepos_yx = nbp_basic.tilepos_yx
    n_rows, n_cols = np.max(tilepos_yx[:, 0]) + 1, np.max(tilepos_yx[:, 1]) + 1
    tilepos_yx = tilepos_yx[nbp_basic.use_tiles]
    diff = np.zeros((n_rows, n_cols))

    for t in use_tiles:
        diff[tilepos_yx[t, 1], tilepos_yx[t, 0]] = residual[t]

    diff = np.flip(diff.T, axis=-1)

    return diff


# 2
def view_round_scales(nbp_register_debug: NotebookPage, nbp_basic: NotebookPage):
    """
    view scale parameters for the round outlier removals
    Args:
        nbp_register_debug: register debug notebook page
        nbp_basic : basic ingo notebook page
    """
    anchor_round, anchor_channel = nbp_basic.anchor_round, nbp_basic.anchor_channel
    use_tiles = nbp_basic.use_tiles
    # Extract raw scales
    z_scale = nbp_register_debug.round_transform_unregularised[use_tiles, :, 0, 0]
    y_scale = nbp_register_debug.round_transform_unregularised[use_tiles, :, 1, 1]
    x_scale = nbp_register_debug.round_transform_unregularised[use_tiles, :, 2, 2]
    n_tiles_use, n_rounds = z_scale.shape[0], z_scale.shape[1]
    
    # Plot box plots
    plt.subplot(3, 1, 1)
    plt.scatter(np.tile(np.arange(n_rounds), n_tiles_use), np.reshape(z_scale, (n_tiles_use * n_rounds)),
                c='w', marker='x')
    plt.plot(np.arange(n_rounds), np.percentile(z_scale, 25, axis=0), 'c:', label='Inter Quartile Range')
    plt.plot(np.arange(n_rounds), np.percentile(z_scale, 50, axis=0), 'r:', label='Median')
    plt.plot(np.arange(n_rounds), np.percentile(z_scale, 75, axis=0), 'c:')
    plt.xlabel('Rounds')
    plt.ylabel('Z-scales')
    plt.legend()

    plt.subplot(3, 1, 2)
    plt.scatter(x=np.tile(np.arange(n_rounds), n_tiles_use),
                y=np.reshape(y_scale, (n_tiles_use * n_rounds)), c='w', marker='x', alpha=0.7)
    plt.plot(np.arange(n_rounds), 0.999 * np.ones(n_rounds), 'c:', label='0.999 - 1.001')
    plt.plot(np.arange(n_rounds), np.ones(n_rounds), 'r:', label='1')
    plt.plot(np.arange(n_rounds), 1.001 * np.ones(n_rounds), 'c:')
    plt.xlabel('Rounds')
    plt.ylabel('Y-scales')
    plt.legend()

    plt.subplot(3, 1, 3)
    plt.scatter(x=np.tile(np.arange(n_rounds), n_tiles_use),
                y=np.reshape(x_scale, (n_tiles_use * n_rounds)), c='w', marker='x', alpha=0.7)
    plt.plot(np.arange(n_rounds), 0.999 * np.ones(n_rounds), 'c:', label='0.999 - 1.001')
    plt.plot(np.arange(n_rounds), np.ones(n_rounds), 'r:', label='1')
    plt.plot(np.arange(n_rounds), 1.001 * np.ones(n_rounds), 'c:')
    plt.xlabel('Rounds')
    plt.ylabel('X-scales')
    plt.legend()

    plt.suptitle('Distribution of scales across tiles for registration from Anchor (R: ' + str(anchor_round)
                 + ', C: ' + str(anchor_channel) + ') to the reference channel of imaging rounds (R: r, C: '
                 + str(anchor_channel) + ') for all rounds r.')
    plt.show()


# 2
def view_channel_scales(nbp_register_debug: NotebookPage, nbp_basic: NotebookPage):
    """
    view scale parameters for the round outlier removals
    Args:
        nbp_register_debug: register debug notebook page
        nbp_basic : basic ingo notebook page
    """
    mid_round, anchor_channel = nbp_basic.n_rounds // 2, nbp_basic.anchor_channel
    use_tiles = nbp_basic.use_tiles
    use_channels = nbp_basic.use_channels
    # Extract raw scales
    z_scale = nbp_register_debug.channel_transform_unregularised[use_tiles, use_channels, 0, 0]
    y_scale = nbp_register_debug.channel_transform_unregularised[use_tiles, use_channels, 1, 1]
    x_scale = nbp_register_debug.channel_transform_unregularised[use_tiles, use_channels, 2, 2]
    n_tiles_use, n_channels_use = z_scale.shape[0], z_scale.shape[1]

    # Plot box plots
    plt.subplot(3, 1, 1)
    plt.scatter(np.tile(np.arange(n_channels_use), n_tiles_use), np.reshape(z_scale, (n_tiles_use * n_channels_use)),
                c='w', marker='x')
    plt.plot(np.arange(n_channels_use), 0.99 * np.ones(n_channels_use), 'c:', label='0.99 - 1.01')
    plt.plot(np.arange(n_channels_use), np.ones(n_channels_use), 'r:', label='1')
    plt.plot(np.arange(n_channels_use), 1.01 * np.ones(n_channels_use), 'c:')
    plt.xticks(np.arange(n_channels_use), use_channels)
    plt.xlabel('Channel')
    plt.ylabel('Z-scale')
    plt.legend()

    plt.subplot(3, 1, 2)
    plt.scatter(x=np.tile(np.arange(n_channels_use), n_tiles_use),
                y=np.reshape(y_scale, (n_tiles_use * n_channels_use)), c='w', marker='x', alpha=0.7)
    plt.plot(np.arange(n_channels_use), np.percentile(y_scale, 25, axis=0), 'c:', label='Inter Quartile Range')
    plt.plot(np.arange(n_channels_use), np.percentile(y_scale, 50, axis=0), 'r:', label='Median')
    plt.plot(np.arange(n_channels_use), np.percentile(y_scale, 75, axis=0), 'c:')
    plt.xticks(np.arange(n_channels_use), use_channels)
    plt.xlabel('Channel')
    plt.ylabel('Y-scale')
    plt.legend()

    plt.subplot(3, 1, 3)
    plt.scatter(x=np.tile(np.arange(n_channels_use), n_tiles_use),
                y=np.reshape(x_scale, (n_tiles_use * n_channels_use)), c='w', marker='x', alpha=0.7)
    plt.plot(np.arange(n_channels_use), np.percentile(x_scale, 25, axis=0), 'c:', label='Inter Quartile Range')
    plt.plot(np.arange(n_channels_use), np.percentile(x_scale, 50, axis=0), 'r:', label='Median')
    plt.plot(np.arange(n_channels_use), np.percentile(x_scale, 75, axis=0), 'c:')
    plt.xticks(np.arange(n_channels_use), use_channels)
    plt.xlabel('Channel')
    plt.ylabel('X-scale')
    plt.legend()

    plt.suptitle('Distribution of scales across tiles for registration from adjusted anchor in coordinate frame of (R: '
                 + str(mid_round) + ', C: ' + str(anchor_channel) + ') to (R:' + str(mid_round) + ' C: c for all '
                                                                                                  'channels c.')
    plt.show()


# 3
def view_icp_n_matches(nb: Notebook, t):
    """
    Plots simple proportion matches against iterations.
    Args:
        nb: Notebook
        t: tile
    """
    nbp_basic, nbp_register_debug, nbp_find_spots = nb.basic_info, nb.register_debug, nb.find_spots
    use_tiles, use_rounds, use_channels = nbp_basic.use_tiles, nbp_basic.use_rounds, nbp_basic.use_channels
    n_matches = nbp_register_debug.n_matches[t, use_rounds, use_channels]
    frac_matches = n_matches_to_frac_matches(nbp_basic=nbp_basic, n_matches=n_matches, spot_no=nbp_find_spots.spot_no)
    n_iters = n_matches.shape[2]

    # Define the axes
    fig, axes = plt.subplots(len(use_rounds), len(use_channels))
    # common axis labels
    fig.supxlabel('Channels')
    fig.supylabel('Rounds')
    # Set row and column labels
    for ax, col in zip(axes[0], use_channels):
        ax.set_title(col)
    for ax, row in zip(axes[:, 0], use_rounds):
        ax.set_ylabel(row, rotation=0, size='large')

    # Now plot n_matches
    for r in range(len(use_rounds)):
        for c in range(len(use_channels)):
            ax = axes[r, c]
            ax.plot(np.arange(n_iters), frac_matches[use_rounds[r], use_channels[c]])
            ax.set_xticks([])
            ax.set_yticks([0, 1])

    plt.suptitle('Fraction of imaging spots matched against iteration of ICP for tile ' + str(t) +
                 ' for all rounds and channels, for all ' + str(n_iters) + ' iterations.')
    plt.show()


# 3
def view_icp_mse(nb: Notebook, t):
    """
    Plots simple MSE grid against iterations
    Args:
        nb: Notebook
        t: tile
    """
    nbp_basic, nbp_register_debug = nb.basic_info, nb.register_debug
    use_tiles, use_rounds, use_channels = nbp_basic.use_tiles, nbp_basic.use_rounds, nbp_basic.use_channels
    mse = nbp_register_debug.mse[t, use_rounds, use_channels]
    n_iters = mse.shape[2]

    # Define the axes
    fig, axes = plt.subplots(len(use_rounds), len(use_channels))
    # common axis labels
    fig.supxlabel('Channels')
    fig.supylabel('Rounds')
    # Set row and column labels
    for ax, col in zip(axes[0], use_channels):
        ax.set_title(col)
    for ax, row in zip(axes[:, 0], use_rounds):
        ax.set_ylabel(row, rotation=0, size='large')

    # Now plot n_matches
    for r in range(len(use_rounds)):
        for c in range(len(use_channels)):
            ax = axes[r, c]
            ax.plot(np.arange(n_iters), mse[use_rounds[r], use_channels[c]])
            ax.set_xticks([])
            ax.set_yticks([np.min(mse[use_rounds[r], use_channels[c]]), np.max(mse[use_rounds[r], use_channels[c]])])

    plt.suptitle('MSE against iteration of ICP for tile ' + str(t) + ' for all rounds and channels, for all '
                 + str(n_iters) + ' iterations.')
    plt.show()
