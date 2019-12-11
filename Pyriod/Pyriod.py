#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
This is Pyriod, a Python package for selecting and fitting sinusoidal signals 
to astronomical time series data.

Written by Keaton Bell

For more, see https://github.com/keatonb/Pyriod

---------------------

# Distinguish clicks with drag motions
# From ImportanceOfBeingErnest
# https://stackoverflow.com/questions/48446351/distinguish-button-press-event-from-drag-and-zoom-clicks-in-matplotlib
    
---------------------

Below here are just some author's notes to keep track of style decisions.

Currently overhauling the periodogram display.
Should be optional to toggle display of each type of periodogram
and their display colors
and which one the mouse is selecting on.

Names of periodograms:
    per_orig
    per_resid
    per_model
    per_sw
    per_markers

Names of associates timeseries:
    lc_orig
    lc_resid
    lc_model_sampled (evenly sampled through gaps)
    lc_model_observed (original time samples)
TODO: rename all lc to ts

Names of plots are:
    lcplot_data,lcplot_model (different nicknames)
    perplot_orig (same nicknames)
    _perplot_orig_display toggle
    _perplot_orig_color picker widget
    
    
What to do about units:
    values dataframe has same units as timeseries
    displayed signals table is in requested units (same as periodogram plot)
    amplitude units implemented, time units not

Decide:
    interppls exists only for inferring amplitude guesses for combo frequencies

TODO: Generate model light curves from lmfit model always (including initialization)

TODO: Table interactions: save, load, delete rows

TODO: Show smoothed light curve (and when folded)

TODO: (re-)generate all periodograms in function

TODO: Fold time series at frequency

Note, Oct 17, 2019: plot can show multiple simultaneous periodograms (not 
spectral window).  Inconsistent tracking of amplitude units causes trouble.

"""

from __future__ import division, print_function
 
import sys
import numpy as np
import itertools
import re
import pandas as pd
from scipy.interpolate import interp1d
import astropy.units as u
from astropy.stats import LombScargle
import lightkurve as lk
from lmfit import Model, Parameters
#from lmfit.models import ConstantModel
from IPython.display import display
import matplotlib.pyplot as plt 
import ipywidgets as widgets
from ipywidgets import HBox,VBox
import qgrid
import logging
if sys.version_info < (3, 0):
    from io import BytesIO as StringIO
else:
    from io import StringIO

from .pyquist import subfreq

plt.ioff()

#Definition of the basic model we fit
def sin(x, freq, amp, phase):
    """for fitting to time series"""
    return amp*np.sin(2.*np.pi*(freq*x+phase))

class Pyriod(object):
    """Time series periodic analysis class.
    
    Attributes
    ----------
    time : array-like
        Time values
    flux : array-like
        Flux values (normalized, mean subtracted)
    
	Future Development
	----------
	Include flux uncertainties, units, etc.
    """
    id_generator = itertools.count(0)
    def __init__(self, lc=None, time=None, flux=None, freq_unit=u.microHertz, 
                 time_unit=u.day, oversample_factor=10, amp_unit='ppt'):
        self.id = next(self.id_generator)
        self.oversample_factor = oversample_factor
        self.freq_unit = freq_unit
        self.freq_conversion = time_unit.to(1/self.freq_unit)
        self.amp_unit = amp_unit
        self.amp_conversion = {'relative':1e0, 'percent':1e2, 'ppt':1e3, 'ppm':1e6}[self.amp_unit]
        
        ### LOG ###
        #Initialize this first and keep track of every important action taken
        self._init_log()
        
        ### TIME SERIES ###
        # Four to keep track of (called lc_nickname)
        # Original (orig), Residuals (resid), 
        # Model (oversampled: model_sampled; and observed: model_observed)
        # Each is lightkurve object
        
        #Store light curve as LightKurve object
        if lc is None and time is None and flux is None:
            raise ValueError('lc or time and flux are required')
        if lc is not None:
            if lk.lightcurve.LightCurve not in type(lc).__mro__:
                raise ValueError('lc must be lightkurve object')
            else:
                self.lc_orig = lc
        else:
            self.lc_orig = lk.LightCurve(time=time, flux=flux)
        
        #Apply time shift to get phases to be well behaved
        self.tshift = -np.mean(self.lc_orig.time)
        
        #Initialize time series widgets and plots
        self._init_timeseries_widgets()
        self.lcfig,self.lcax = plt.subplots(figsize=(6,2),num='Time Series ({:d})'.format(self.id))
        self.lcax.set_xlabel("time")
        self.lcax.set_ylabel("rel. variation")
        self.lcplot_orig, = self.lcax.plot(self.lc_orig.time,self.lc_orig.flux,marker='o',ls='None',ms=1)
        #Also plot the model over the time series
        dt = np.median(np.diff(self.lc_orig.time))
        time_samples = np.arange(np.min(self.lc_orig.time),
                                 np.max(self.lc_orig.time)+dt,dt)
        initmodel = np.zeros(len(time_samples))+np.mean(self.lc_orig.flux)
        self.lc_model_sampled = lk.LightCurve(time=time_samples,flux=initmodel)
        initmodel = np.zeros(len(self.lc_orig.time))+np.mean(self.lc_orig.flux)
        self.lc_model_observed = lk.LightCurve(time=self.lc_orig.time,flux=initmodel)
        
        self.lcplot_model, = self.lcax.plot(self.lc_model_sampled.time,
                                            self.lc_model_sampled.flux,c='r',lw=1)
        plt.tight_layout()
        
        #And keep track of residuals time series
        self.lc_resid = self.lc_orig - self.lc_model_observed
        
        
        ### PERIODOGRAM ###
        # Four types for display
        # Original (orig), Residuals (resid), Model (model), and Spectral Window (sw)
        # Each is stored as, e.g., "per_orig", samples at self.freqs
        # Has associated plot _perplot_orig
        # Display toggle widget _perplot_orig_display
        # And color picker _perplot_orig_color
        
        #Initialize widgets
        self._init_periodogram_widgets()
        
        #Set up some figs/axes for periodogram plots
        self.perfig,self.perax = plt.subplots(figsize=(6,3),num='Periodogram ({:d})'.format(self.id))
        self.perax.set_xlabel("frequency")
        self.perax.set_ylabel("amplitude ({})".format(self.amp_unit))
        plt.tight_layout()
        
        
        #Define frequency sampling
        
        #Determine frequency resolution
        self.fres = 1./(self.lc_orig.time[-1]-self.lc_orig.time[0])
        self._fold_on.step = self.fres #let fold_on step by freq res
        #And the Nyquist (approximate for unevenly sampled data)
        self.nyq = 1./(2.*dt*self.freq_conversion)
        #Sample the following frequencies:
        self.freqs = np.arange(0,self.nyq+self.fres/oversample_factor,self.fres/oversample_factor)
        
        #Compute and plot original periodogram
        self.per_orig = self.lc_orig.to_periodogram(normalization='amplitude',freq_unit=freq_unit,
                                               frequency=self.freqs)*self.amp_conversion
        self.perplot_orig, = self.perax.plot(self.freqs,self.per_orig.power.value,lw=1)
        self.perax.set_xlabel("frequency ({})".format(self.per_orig.frequency.unit.to_string()))
        self.perax.set_ylim(0,1.05*np.nanmax(self.per_orig.power.value))
        
        #Compute and plot periodogram of model sampled as observed
        self.per_model = self.lc_model_observed.to_periodogram(normalization='amplitude',freq_unit=freq_unit,
                                               frequency=self.freqs).power.value*self.amp_conversion
        self.perplot_model, = self.perax.plot(self.freqs,self.per_model,lw=1)

        #Compute and plot periodogram of residuals
        self.per_resid = self.lc_resid.to_periodogram(normalization='amplitude',freq_unit=freq_unit,
                                               frequency=self.freqs).power.value*self.amp_conversion
        self.perplot_resid, = self.perax.plot(self.freqs,self.per_resid,lw=1)
                                         
        #Compute spectral window
        #TODO: do with lightkurve
        #May not work in Python3!!
        self.specwin = np.sqrt(LombScargle(self.lc_orig.time*self.freq_conversion, np.ones(self.lc_orig.time.shape),
                                           fit_mean=False).power(self.freqs,method = 'fast'))
        #self.perplot_sw, = self.perax.plot(self.freqs,self.specwin,lw=1)
        
        #Is the following truly needed?
        self.interpls = interp1d(self.freqs,self.per_orig.power.value)
        
        #Create markers for selected peak, adopted signals
        self.marker = self.perax.plot([0],[0],c='k',marker='o')[0]
        self.signal_marker_color = 'green'
        self.signal_markers, = self.perax.plot([],[],marker='D',fillstyle='none',
                                               linestyle='None',
                                               c=self.signal_marker_color,ms=5)
        #self._makeperiodsolutionvisible()
        self._display_per_orig()
        self._display_per_resid()
        self._display_per_model()
        self._display_per_sw()
        self._display_per_markers()
        
        
        self.update_marker(self.freqs[np.nanargmax(self.per_orig.power.value)],
                           np.nanmax(self.per_orig.power.value))
        
        
        #This handles clicking while zooming problems
        #self.perfig.canvas.mpl_connect('button_press_event', self.onperiodogramclick)
        self._press= False
        self._move = False
        self.perfig.canvas.mpl_connect('button_press_event', self.onpress)
        self.perfig.canvas.mpl_connect('button_release_event', self.onrelease)
        self.perfig.canvas.mpl_connect('motion_notify_event', self.onmove)
        
        
        
        ### SIGNALS ###
        
        #Hold signal phases, frequencies, and amplitudes in Pandas DF
        self.values = self.initialize_dataframe()
        
        #self.uncertainties = pd.DataFrame(columns=self.columns[::2]) #not yet used
        
        #The interface for interacting with the values DataFrame:
        self._init_signals_qgrid()
        self.signals_qgrid = self.get_qgrid()
        self.signals_qgrid.on('cell_edited', self._qgrid_changed_manually)
        self._init_signals_widgets()
        
        
        self.log("Pyriod object initialized.")
    
    
    ###### Run initialization functions #######
    
    
    def _init_timeseries_widgets(self):
        ### Time Series widget stuff  ###
        self._tstype = widgets.Dropdown(
            options=['Original', 'Residuals'],
            value='Original',
            description='Time Series to Display:',
            disabled=False
        )
        self._tstype.observe(self._update_lc_display)
        
        self._fold = widgets.Checkbox(
            value=False,
            description='Fold time series on frequency?',
        )
        self._fold.observe(self._update_lc_display)
        
        self._fold_on = widgets.FloatText(
            value=1.,
            description='Fold on freq:'
        )
        self._fold_on.observe(self._update_lc_display)
        
        self._select_fold_freq = widgets.Dropdown(
            description='Select from:',
            disabled=False,
        )
        self._select_fold_freq.observe(self._fold_freq_selected,'value')
    
    def _init_periodogram_widgets(self):
        ### Periodogram widget stuff  ###
        self._thisfreq = widgets.Text(
            value='',
            placeholder='',
            description='Frequency:',
            disabled=False
        )
        
        
        self._thisamp = widgets.FloatText(
            value=0.001,
            #min=0,
            #max=np.max(amp),
            #step=None,
            description='Amplitude:',
            disabled=False
        )
        
        ##Needed???
        #self._recalculate = widgets.Button(
        #    description='Recalculate',
        #    disabled=True,
        #    button_style='', # 'success', 'info', 'warning', 'danger' or ''
        #    tooltip='Click to recalculate periodogram based on updated solution.',
        #    icon='refresh'
        #)
        
        
        
        self._addtosol = widgets.Button(
            description='Add to solution',
            disabled=False,
            button_style='success', # 'success', 'info', 'warning', 'danger' or ''
            tooltip='Click to add currently selected values to frequency solution',
            icon='plus'
        )
        self._addtosol.on_click(self._add_staged_signal)
        
        self._snaptopeak = widgets.Checkbox(
            value=True,
            description='Snap clicks to peaks?',
            disabled=False
        )
        
        self._show_per_markers = widgets.Checkbox(
            value=True,
            description='Signal Markers',
            disabled=False,
            style={'description_width': 'initial'}
        )
        self._show_per_markers.observe(self._display_per_markers)
        
        #Check boxes for what to include on periodogram plot
        self._show_per_orig = widgets.Checkbox(
            value=True,
            description='Original',
            disabled=False,
            style={'description_width': 'initial'}
        )
        self._show_per_orig.observe(self._display_per_orig)
        
        self._show_per_resid = widgets.Checkbox(
            value=False,
            description='Residuals',
            disabled=False,
            style={'description_width': 'initial'}
        )
        self._show_per_resid.observe(self._display_per_resid)
        
        self._show_per_model = widgets.Checkbox(
            value=False,
            description='Model',
            disabled=False,
            style={'description_width': 'initial'}
        )
        self._show_per_model.observe(self._display_per_model)
        
        self._show_per_sw = widgets.Checkbox(
            value=False,
            description='Spectral Window (disabled)',
            disabled=True,
            style={'description_width': 'initial'}
        )
        self._show_per_sw.observe(self._display_per_sw)
    
    def _init_signals_qgrid(self):
        #Set some options for how the qgrid of values should be displayed
        self._gridoptions = {
                # SlickGrid options
                'fullWidthRows': True,
                'syncColumnCellResize': True,
                'forceFitColumns': False,
                'defaultColumnWidth': 65,  #control col width (all the same)
                'rowHeight': 28,
                'enableColumnReorder': False,
                'enableTextSelectionOnCells': True,
                'editable': True,
                'autoEdit': True, #double-click not required!
                'explicitInitialization': True,
                
    
                # Qgrid options
                'maxVisibleRows': 15,
                'minVisibleRows': 8,
                'sortable': True,
                'filterable': False,  #Not useful here
                'highlightSelectedCell': False,
                'highlightSelectedRow': True
               }
        
        self._column_definitions = {"include":  {'width': 65, 'toolTip': "include signal in model fit?"},
                                    "freq":      {'width': 130, 'toolTip': "mode frequency"},
                                    "fixfreq":  {'width': 65, 'toolTip': "fix frequency during fit?"},
                                    "freqerr":  {'width': 130, 'toolTip': "uncertainty on frequency", 'editable': False},
                                    "amp":       {'width': 130, 'toolTip': "mode amplitude"},
                                    "fixamp":   {'width': 65, 'toolTip': "fix amplitude during fit?"},
                                    "amperr":  {'width': 130, 'toolTip': "uncertainty on amplitude", 'editable': False},
                                    "phase":     {'width': 130, 'toolTip': "mode phase"},
                                    "fixphase": {'width': 65, 'toolTip': "fix phase during fit?"},
                                    "phaseerr":  {'width': 130, 'toolTip': "uncertainty on phase", 'editable': False}}
    
    def _init_signals_widgets(self):
        ### Time Series widget stuff  ###
        self._refit = widgets.Button(
            description="Refine fit",
            disabled=False,
            #button_style='success', # 'success', 'info', 'warning', 'danger' or ''
            tooltip='Refine fit of signals to time series',
            icon='refresh'
        )
        self._refit.on_click(self.fit_model)
        
        self._delete = widgets.Button(
            description='Delete selected',
            disabled=False,
            button_style='danger', # 'success', 'info', 'warning', 'danger' or ''
            tooltip='Delete selected rows.',
            icon='trash'
        )
        self._delete.on_click(self._delete_selected)
    
        
    def _init_log(self):
        #To log messages, use self.log() function
        
        self.logger = logging.getLogger('basic_logger')
        self.logger.setLevel(logging.DEBUG)
        self.log_capture_string = StringIO()
        ch = logging.StreamHandler(self.log_capture_string)
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        ch.setFormatter(formatter)
        self.logger.addHandler(ch)
        
        self._log = widgets.HTML(
            value='Log',
            placeholder='Log',
            description='Log:',
            layout={'height': '100%',
                    'width': '90%'}
        )
        self._logbox = widgets.VBox([self._log], layout={'height': '200px','width': '950px'})
    
    #Function for logging messages
    def log(self,message,level='info'):
        logdict = {
            'debug': self.logger.debug,
            'info': self.logger.info,
            'warn': self.logger.warn,
            'error': self.logger.error,
            'critical': self.logger.critical
            }
        logdict[level](message+'<br>')
        self._update_log()
        
    
    #Functions for interacting with model fit
    def add_signal(self, freq, amp=None, phase=None, fixfreq=False, 
                   fixamp=False, fixphase=False, include=True, index=None):
        if amp is None:
            amp = 1.
        if phase is None:
            phase = 0.5
        #list of iterables required to pass to dataframe without an index
        newvalues = [[nv] for nv in [freq,fixfreq,amp/self.amp_conversion,fixamp,phase,fixphase,include]]
        colnames = ["freq","fixfreq","amp","fixamp","phase","fixphase","include"]
        if index == None:
            #TODO: fix numbering to find next indep frequency number
            index = "f{}".format(len(self.values))
        toappend = pd.DataFrame(dict(zip(colnames,newvalues)),columns=self.columns,
                                index=[index])
        self.values = self.values.append(toappend,sort=False)
        self._update_freq_dropdown() #For folding time series
        displayframe = self.values.copy()[self.columns[:-1]]
        displayframe["amp"] = displayframe["amp"] * self.amp_conversion
        self.signals_qgrid.df = displayframe.combine_first(self.signals_qgrid.df)[self.columns[:-1]] #Update displayed values
        #self.signals_qgrid.df.columns = self.columns[:-1]
        self._update_signal_markers()
        self.log("Signal {} added to model with frequency {} and amplitude {}.".format(index,freq,amp))
        
    #operators = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
    #             ast.Div: op.truediv,ast.USub: op.neg}
    def add_combination(self, combostr, amp=None, phase=None, fixfreq=False, 
                   fixamp=False, fixphase=False, index=None):
        combostr = combostr.replace(" ", "")
        #evaluate combostring:
        
        #replace keys with values
        parts = re.split('\+|\-|\*|\/',combostr)
        keys = set([part for part in parts if part in self.values.index])
        expression = combostr
        for key in keys:
            expression = expression.replace(key, str(self.values.loc[key,'freq']))
        freqval = eval(expression)
        if amp == None:
            amp = self.interpls(subfreq(freqval,self.nyq)[0])
        self.add_signal(freqval,amp,index=combostr)
        self.log("Combination {} added to model.".format(combostr))
        
    
    def fit_model(self, *args):
        """ 
        Update model to include current signals from DataFrame.
        
        Improve fit once with all frequencies fixed, then allow to vary.
        """
        if np.sum(self.values.include.values) == 0:
            return #If nothing to fit
        
        #Set up lmfit model for fitting
        signals = {} #empty dict to be populated
        params = Parameters()
        
        #handle combination frequencies differently
        isindep = lambda key: key[1:].isdigit()
        cnum = 0
        
        prefixmap = {}
        
        #first with frequencies fixed
        #for those specified to be included in the model
        for prefix in self.values.index[self.values.include]:
            #prefix = 'f{}'.format(i+1)
            #freqkeys.append(prefix)
                if isindep(prefix):
                    signals[prefix] = Model(sin,prefix=prefix)
                    params.update(signals[prefix].make_params())
                    params[prefix+'freq'].set(self.freq_conversion*self.values.freq[prefix], vary=False)
                    params[prefix+'amp'].set(self.values.amp[prefix], vary=~self.values.fixamp[prefix])
                    params[prefix+'phase'].set(self.values.phase[prefix], vary=~self.values.fixphase[prefix])
                    prefixmap[prefix] = prefix
                else: #combination
                    useprefix = 'c{}'.format(cnum)
                    signals[useprefix] = Model(sin,prefix=useprefix)
                    params.update(signals[useprefix].make_params())
                    parts = re.split('\+|\-|\*|\/',prefix)
                    keys = set([part for part in parts if part in self.values.index])
                    expression = prefix
                    for key in keys:
                        expression = expression.replace(key, key+'freq')
                    params[useprefix+'freq'].set(expr=expression)
                    params[useprefix+'amp'].set(self.values.amp[prefix], vary=~self.values.fixamp[prefix])
                    params[useprefix+'phase'].set(self.values.phase[prefix], vary=~self.values.fixphase[prefix])
                    prefixmap[prefix] = useprefix
                    cnum+=1
        
        #model is sum of sines
        model = np.sum([signals[prefixmap[prefix]] for prefix in self.values.index[self.values.include]])
        
        #compute fixed-frequency fit
        result = model.fit(self.lc_orig.flux-np.mean(self.lc_orig.flux), params, x=self.lc_orig.time+self.tshift)
        
        #refine, allowing freq to vary (unless fixed by user)
        params = result.params
        
        for prefix in self.values.index[self.values.include]:
            if isindep(prefix):
                params[prefixmap[prefix]+'freq'].set(vary=~self.values.fixfreq[prefix])
                params[prefixmap[prefix]+'amp'].set(result.params[prefixmap[prefix]+'amp'].value)
                params[prefixmap[prefix]+'phase'].set(result.params[prefixmap[prefix]+'phase'].value)
                
        result = model.fit(self.lc_orig.flux-np.mean(self.lc_orig.flux), params, x=self.lc_orig.time+self.tshift)
        
        self._update_values_from_fit(result.params,prefixmap)
        self.log("Fit refined.")
        
    def _update_values_from_fit(self,params,prefixmap):
        #update dataframe of params with new values from fit
        #also rectify and negative amplitudes or phases outside [0,1)
        #isindep = lambda key: key[1:].isdigit()
        #cnum = 0
        for prefix in self.values.index[self.values.include]:
            self.values.loc[prefix,'freq'] = float(params[prefixmap[prefix]+'freq'].value/self.freq_conversion)
            self.values.loc[prefix,'freqerr'] = float(params[prefixmap[prefix]+'freq'].stderr/self.freq_conversion)
            self.values.loc[prefix,'amp'] = params[prefixmap[prefix]+'amp'].value
            self.values.loc[prefix,'amperr'] = float(params[prefixmap[prefix]+'amp'].stderr)
            self.values.loc[prefix,'phase'] = params[prefixmap[prefix]+'phase'].value
            self.values.loc[prefix,'phaseerr'] = float(params[prefixmap[prefix]+'phase'].stderr)
            #rectify
            if self.values.loc[prefix,'amp'] < 0:
                self.values.loc[prefix,'amp'] *= -1.
                self.values.loc[prefix,'phase'] -= 0.5
            #Reference phase to t0
            self.values.loc[prefix,'phase'] += self.tshift*self.values.loc[prefix,'freq']*self.freq_conversion
            self.values.loc[prefix,'phase'] %= 1.
        self._update_freq_dropdown()
        
        #update qgrid
        self.signals_qgrid.df = self._convert_values_to_qgrid().combine_first(self.signals_qgrid.df)[self.columns[:-1]]
        #self.signals_qgrid.df = self._convert_values_to_qgrid()[self.columns[:-1]]
        #TODO: also update uncertainties
        
        self._update_values_from_qgrid() #Necessary?
    
    def _convert_values_to_qgrid(self):
        tempdf = self.values.copy()[self.columns[:-1]]
        tempdf["amp"] *= self.amp_conversion
        tempdf["amperr"] *= self.amp_conversion
        return tempdf
    
    def _convert_qgrid_to_values(self):
        tempdf = self.signals_qgrid.get_changed_df().copy()
        tempdf["amp"] /= self.amp_conversion
        tempdf["amperr"] /= self.amp_conversion
        return tempdf
    
    def _update_values_from_qgrid(self):# *args
        self.values = self._convert_qgrid_to_values()
        
        self._update_lcs()
        self._update_signal_markers()
        self._update_lc_display()
        self._update_pers()
        
    def _update_lcs(self):
        #Update time series models
        self.lc_model_sampled.flux = np.zeros(len(self.lc_model_sampled))+np.mean(self.lc_orig.flux)
        self.lc_model_observed.flux = np.zeros(len(self.lc_orig.time))+np.mean(self.lc_orig.flux)
        
        for prefix in self.values.index:
            freq = float(self.values.loc[prefix,'freq'])
            amp = float(self.values.loc[prefix,'amp'])
            phase = float(self.values.loc[prefix,'phase'])
            self.lc_model_sampled += sin(self.lc_model_sampled.time,
                                              freq*self.freq_conversion,amp,phase)
            self.lc_model_observed += sin(self.lc_model_observed.time,
                                               freq*self.freq_conversion,amp,phase)
        self.lc_resid = self.lc_orig - self.lc_model_observed
    
    def _qgrid_changed_manually(self, *args):
        #note: args has information about what changed if needed
        newdf = self.signals_qgrid.get_changed_df()
        olddf = self.signals_qgrid.df
        logmessage = "Signals table changed manually.\n"
        for key in newdf.index.values:
            if key in olddf.index.values:
                changes = newdf.loc[key][olddf.loc[key] != newdf.loc[key]]
                if len(changes > 0):
                    logmessage += "Values changed for {}:\n".format(key)
                for change in changes.index:
                    logmessage += " - {} -> {}\n".format(change,changes[change])
            else:
                logmessage += "New row in solution table: {}\n".format(key)
                for col in newdf.loc[key]:
                    logmessage += " - {} -> {}\n".format(change,changes[change])
        self.log(logmessage)
        self.signals_qgrid.df = self.signals_qgrid.get_changed_df().combine_first(self.signals_qgrid.df)[self.columns[:-1]]
        #self.signals_qgrid.df.columns = self.columns[:-1]
        self._update_values_from_qgrid()
    
    columns = ['include','freq','fixfreq','freqerr',
               'amp','fixamp','amperr',
               'phase','fixphase','phaseerr','combo']
    dtypes = ['bool','object','bool','float',
              'float','bool','float',
              'float','bool','float','bool']
    
    def delete_rows(self,indices):
        self.values = self.values.drop(indices)
        self.signals_qgrid.df = self.signals_qgrid.df.drop(indices)
    
    def _delete_selected(self, *args):
        self.delete_rows(self.signals_qgrid.get_selected_df().index)
        self._update_freq_dropdown()
        self._update_signal_markers()
    
    def initialize_dataframe(self):
        df = pd.DataFrame(columns=self.columns).astype(dtype=dict(zip(self.columns,self.dtypes)))
        return df
    
    
    #Stuff for folding the light curve on a certain frequency
    def _fold_freq_selected(self,value):
        if value['new'] is not None:
            self._fold_on.value = value['new']
        
    def _update_freq_dropdown(self):
        labels = [self.values.index[i] + ': {:.8f} '.format(self.values.freq[i]) + self.per_orig.frequency.unit.to_string() for i in range(len(self.values))]
        currentind = self._select_fold_freq.index
        if currentind == None:
            currentind = 0
        if len(labels) == 0:
            self._select_fold_freq.options = [None]
        else:
            self._select_fold_freq.options = zip(labels, self.values.freq.values)
            self._select_fold_freq.index = currentind
        
        
    ########## Set up *SIGNALS* widget using qgrid ##############
    
    
        
    
    def get_qgrid(self):
        display_df = self.values[self.columns[:-1]].copy()
        display_df["amp"] *= self.amp_conversion
        display_df["amperr"] *= self.amp_conversion
        return qgrid.show_grid(display_df, show_toolbar=False, precision = 9,
                               grid_options=self._gridoptions,
                               column_definitions=self._column_definitions)
    
    #add staged signal to frequency solution
    def _add_staged_signal(self, *args):
        #Is this a valid numeric frequency?
        if self._thisfreq.value.replace('.','',1).isdigit():
            self.add_signal(float(self._thisfreq.value),self._thisamp.value)
        else:
            parts = re.split('\+|\-|\*|\/',self._thisfreq.value.replace(" ", ""))
            allvalid = np.all([(part in self.values.index) or [part.replace('.','',1).isdigit()] for part in parts])
            #Is it a valid combination frequency?
            if allvalid and (len(parts) > 1):
                #will guess amplitude from periodogram
                self.add_combination(self._thisfreq.value)
            #Otherwise issue a warning
            else:
                self.log("Staged frequency has invalid format: {}".format(self._thisfreq.value),"error")
        
    #change type of time series being displayed
    def _update_lc_display(self, *args):
        displaytype = self._tstype.value
        updatedisplay = {"Original":self._display_original_lc,
                         "Residuals":self._display_residuals_lc}
        updatedisplay[displaytype]()
        
        
    def _update_signal_markers(self):
        subnyquistfreqs = subfreq(self.values['freq'].astype('float'),self.nyq)
        self.signal_markers.set_data(subnyquistfreqs,self.values['amp']*self.amp_conversion)
        self.perfig.canvas.draw()
        
    def _display_original_lc(self):
        self.lcplot_orig.set_ydata(self.lc_orig.flux)
        self.lcplot_model.set_ydata(self.lc_model_sampled.flux)
        #rescale y to better match data
        ymin = np.min([np.min(self.lc_orig.flux),np.min(self.lc_model_sampled.flux)])
        ymax = np.max([np.max(self.lc_orig.flux),np.max(self.lc_model_sampled.flux)])
        self.lcax.set_ylim(ymin-0.05*(ymax-ymin),ymax+0.05*(ymax-ymin))
        #fold if requested
        if self._fold.value:
            self.lcplot_orig.set_xdata(self.lc_orig.time*self._fold_on.value*self.freq_conversion % 1.)
            self.lcplot_model.set_alpha(0)
            self.lcax.set_xlim(0,1)
        else:
            self.lcplot_orig.set_xdata(self.lc_orig.time)
            self.lcplot_model.set_alpha(1)
            self.lcax.set_xlim(np.min(self.lc_orig.time),np.max(self.lc_orig.time))
        self.lcfig.canvas.draw()
        
    def _display_residuals_lc(self):
        self.lcplot_orig.set_ydata(self.lc_resid.flux)
        self.lcplot_model.set_ydata(np.zeros(len(self.lc_model_sampled.flux)))
        ymin = np.min(self.lc_resid.flux)
        ymax = np.max(self.lc_resid.flux)
        self.lcax.set_ylim(ymin-0.05*(ymax-ymin),ymax+0.05*(ymax-ymin))
        #fold if requested
        if self._fold.value:
            self.lcplot_orig.set_xdata(self.lc_orig.time*self._fold_on.value*self.freq_conversion % 1.)
            self.lcplot_model.set_alpha(0)
            self.lcax.set_xlim(0,1)
        else:
            self.lcplot_orig.set_xdata(self.lc_orig.time)
            self.lcplot_model.set_alpha(1)
            self.lcax.set_xlim(np.min(self.lc_orig.time),np.max(self.lc_orig.time))
        self.lcfig.canvas.draw()
    
    
    def _update_pers(self):
        self.per_model = self.lc_model_observed.to_periodogram(normalization='amplitude',freq_unit=self.freq_unit,
                                               frequency=self.freqs).power.value*self.amp_conversion
        self.perplot_model.set_ydata(self.per_model)
        self.per_resid = self.lc_resid.to_periodogram(normalization='amplitude',freq_unit=self.freq_unit,
                                               frequency=self.freqs).power.value*self.amp_conversion
        self.perplot_resid.set_ydata(self.per_resid)
        self.perfig.canvas.draw()
   
    def _display_per_orig(self, *args):
        if self._show_per_orig.value:
            self.perplot_orig.set_alpha(1)
        else:
            self.perplot_orig.set_alpha(0)
        self.perfig.canvas.draw()
        
    def _display_per_resid(self, *args):
        if self._show_per_resid.value:
            self.perplot_resid.set_alpha(1)
        else:
            self.perplot_resid.set_alpha(0)
        self.perfig.canvas.draw()
        
    def _display_per_model(self, *args):
        if self._show_per_model.value:
            self.perplot_model.set_alpha(1)
        else:
            self.perplot_model.set_alpha(0)
        self.perfig.canvas.draw()
        
    def _display_per_sw(self, *args):
        #if self._show_per_sw.value:
        #    self.perplot_sw.set_alpha(1)
        #else:
        #    self.perplot_sw.set_alpha(0)
        #self.perfig.canvas.draw()
        pass #temporary
        
    def _display_per_markers(self, *args):
        if self._show_per_markers.value:
            self.signal_markers.set_alpha(1)
        else:
            self.signal_markers.set_alpha(0)
        self.perfig.canvas.draw()
    
    def onperiodogramclick(self,event):
        if self._snaptopeak.value:
            #click within either frequency resolution or 1% of displayed range
            #TODO: make this work with log frequency too
            tolerance = np.max([self.fres,0.01*np.diff(self.perax.get_xlim())])
            
            nearby = np.argwhere((self.freqs >= event.xdata - tolerance) & 
                                 (self.freqs <= event.xdata + tolerance))
            ydata = self.perplot_orig.get_ydata()
            highestind = np.nanargmax(ydata[nearby]) + nearby[0]
            self.update_marker(self.freqs[highestind],ydata[highestind])
        else:
            self.update_marker(event.xdata,self.interpls(event.xdata))
        
    def Periodogram(self):
        #display(#self._pertype,self._recalculate,
        pertab1 = VBox([widgets.HBox([self._thisfreq,self._thisamp]),
                        self._addtosol,
                        self.perfig.canvas])
        pertab2 = VBox([self._snaptopeak,self._show_per_markers,
                        self._show_per_orig,self._show_per_resid,
                        self._show_per_model,self._show_per_sw])
        pertabs = widgets.Tab(children=[pertab1,pertab2])
        pertabs.set_title(0, 'plot')
        pertabs.set_title(1, 'options')
        return pertabs
        
        
    def TimeSeries(self):
        return VBox([self._tstype,self._fold,self._fold_on,self._select_fold_freq,
                     self.lcfig.canvas])
        #display(self._tstype,self.lcfig)
        
    def update_marker(self,x,y):
        try:
            self._thisfreq.value = str(x[0])
        except:
            self._thisfreq.value = str(x)
        self._thisamp.value =  y
        self.marker.set_data([x],[y])
        self.perfig.canvas.draw()
        self.perfig.canvas.flush_events()
        
        
    def onclick(self,event):
        self.onperiodogramclick(event)
    def onpress(self,event):
        self._press=True
    def onmove(self,event):
        if self._press:
            self._move=True
    def onrelease(self,event):
        if self._press and not self._move:
            self.onclick(event)
        self._press=False; self._move=False

    def Signals(self):
        display(widgets.HBox([self._refit,self._thisfreq,self._thisamp,self._addtosol,self._delete]),self.signals_qgrid)
        
    def Log(self):
        display(self._logbox)
    
    def _update_log(self):
        self._log.value = self.log_capture_string.getvalue()
        
    
