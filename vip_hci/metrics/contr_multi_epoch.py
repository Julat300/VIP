# -*- coding: utf-8 -*-
"""
Created on Fri Dec  8 20:00:53 2023

@author: justi
"""

import numpy as np
import pandas as pd
from inspect import getfullargspec
try:
    from photutils.aperture import aperture_photometry, CircularAperture
except:
    from photutils import aperture_photometry, CircularAperture
from scipy.interpolate import InterpolatedUnivariateSpline
from scipy import stats
from scipy.signal import savgol_filter
from skimage.draw import disk
from matplotlib import pyplot as plt
from ..fm import cube_inject_companions, frame_inject_companion, normalize_psf
from ..config import time_ini, timing
from ..config.utils_conf import vip_figsize, vip_figdpi
from ..var import frame_center, dist

from .contrcurve import noise_per_annulus, aperture_flux
from ..psfsub.pca_fullfr import *
from ..psfsub.pca_fullfr import PCA_Params
from ..psfsub.pca_fullfr import PCA_Params
from ..psfsub.pca_local import *
from ..psfsub.pca_local import PCA_ANNULAR_Params
from ..psfsub.pca_multi_epoch import *

from hciplot import plot_frames, plot_cubes

def contrast_optimized(
    cube,
    angle_list,
    psf_template,
    fwhm,
    pxscale,
    starphot,
    algo,
    ncomp,
    cube_delimiter,
    cube_ref_delimiter = None,
    sigma=5,
    nbranch=1,
    distance=2,
    theta=0,
    inner_rad=1,
    fc_rad_sep=3,
    noise_sep=1,
    wedge=(0, 360),
    fc_snr=100,
    student=True,
    transmission=None,
    smooth=True,
    interp_order=2,
    plot=True,
    dpi=vip_figdpi,
    debug=False,
    verbose=True,
    full_output=False,
    save_plot=None,
    object_name=None,
    frame_size=None,
    fix_y_lim=(),
    figsize=vip_figsize,
    algo_class=None,
    **algo_dict
):
    
    if cube.ndim != 3 and cube.ndim != 4:
        raise TypeError("The input array is not a 3d or 4d cube")
    if cube.ndim == 3 and (cube.shape[0] != angle_list.shape[0]):
        raise TypeError("Input parallactic angles vector has wrong length")
    if cube.ndim == 4 and (cube.shape[1] != angle_list.shape[0]):
        raise TypeError("Input parallactic angles vector has wrong length")
    if cube.ndim == 3 and psf_template.ndim != 2:
        raise TypeError("Template PSF is not a frame (for ADI case)")
    if cube.ndim == 4 and psf_template.ndim != 3:
        raise TypeError("Template PSF is not a cube (for ADI+IFS case)")
    if transmission is not None:
        if len(transmission) != 2 and len(transmission) != cube.shape[0] + 1:
            msg = "Wrong shape for transmission should be 2xn_rad or (nch+1) "
            msg += "x n_rad, instead of {}".format(transmission.shape)
            raise TypeError(msg)

    if isinstance(fwhm, (np.ndarray, list)):
        fwhm_med = np.median(fwhm)
    else:
        fwhm_med = fwhm

    if verbose:
        start_time = time_ini()
        if isinstance(starphot, float) or isinstance(starphot, int):
            msg0 = "ALGO : {}, FWHM = {}, # BRANCHES = {}, SIGMA = {},"
            msg0 += " STARPHOT = {}"
            print(msg0.format('pca_annular_multi_epoch', fwhm_med, nbranch, sigma, starphot))
        else:
            msg0 = "ALGO : {}, FWHM = {}, # BRANCHES = {}, SIGMA = {}"
            print(msg0.format('pca_annular_multi_epoch', fwhm_med, nbranch, sigma))

    # throughput
    verbose_thru = False
    if verbose == 2:
        verbose_thru = True
        
        
        
    #Do everything needed on empty cube, only once!
    array = cube
    parangles = angle_list
    nproc = algo_dict.get("nproc", 1)
    imlib = algo_dict.get("imlib", "vip-fft")
    interpolation = algo_dict.get("interpolation", "lanczos4")
    scaling = algo_dict.get("scaling", None)

    if array.ndim != 3 and array.ndim != 4:
        raise TypeError("The input array is not a 3d or 4d cube")

    else:
        if psf_template.shape[1] % 2 == 0:
            raise ValueError("Only odd-sized PSF is accepted")
        if not hasattr(algo, "__call__"):
            raise TypeError("Parameter `algo` must be a callable function")
        if not isinstance(inner_rad, int):
            raise TypeError("inner_rad must be an integer")
        angular_range = wedge[1] - wedge[0]
        if nbranch > 1 and angular_range < 360:
            msg = "Only a single branch is allowed when working on a wedge"
            raise RuntimeError(msg)

    if isinstance(fwhm, (np.ndarray, list)):
        fwhm_med = np.median(fwhm)
    else:
        fwhm_med = fwhm

    if verbose:
        start_time = time_ini()
    # ***************************************************************************
    # Compute noise in concentric annuli on the "empty frame"

    # TODO: Clean below?
    # Consider 3 cases depending on whether algo is (i) defined externally,
    # (ii) a VIP postproc algorithm; (iii) ineligible for contrast curves
    
    
    NEpochs = len(ncomp)
    SizeEpoch = int(cube.shape[0]/NEpochs)
    SizeEpochs = np.ones(NEpochs, dtype = int)
    CompPerE = np.ones(NEpochs, dtype = int)
    FullSize = int(0)
    NCombinations = int(1)
    SizeImage = int(cube[0].shape[0])
    for i in range(0, NEpochs, 1):
        CompPerE[i] = int(len(ncomp[i]))
        FullSize += int(CompPerE[i])
        NCombinations *= int(CompPerE[i])
        SizeEpochs[i] = cube_delimiter[i+1]-cube_delimiter[i]
    DefaultSizeE = np.max(SizeEpochs)
    Res_fc = np.zeros((FullSize, nbranch, DefaultSizeE, SizeImage, SizeImage), dtype = float)
    Res_no_fc = np.zeros((FullSize, DefaultSizeE, SizeImage, SizeImage), dtype = float)
    frames_no_fc = np.zeros((NCombinations, SizeImage, SizeImage))
    noiseF = np.zeros((NCombinations))
    res_levelF = np.zeros((NCombinations))
    fc_maps = np.zeros((NCombinations, SizeImage, SizeImage))
    
    D = int(distance)
    rad_dist = distance * fwhm
    Throughput = np.zeros((NCombinations, nbranch))
    Thru_Cont = np.zeros((NCombinations, 2))
    
    
    class_params, rot_options = separate_kwargs_dict(
        initial_kwargs=algo_dict, parent_class=PCA_ANNULAR_MULTI_EPOCH_Params)
    

    class_params['cube'] = cube
    class_params['angle_list'] = angle_list
    class_params['fwhm'] = fwhm
    class_params['cube_delimiter'] = cube_delimiter
    class_params['cube_ref_delimiter'] = cube_ref_delimiter
    
    all_args = ()
    algo_params = PCA_ANNULAR_MULTI_EPOCH_Params(*all_args, **class_params)
    
    Inherited, NotInherited = Inherited_Params(algo_params)
    
    ToRemove = ['full_output', 'ncomp', 'cube', 'cube_ref', 'angle_list', 'delta_rot']
    Args_left = RemoveKeys(Inherited, ToRemove)
    
    if (type(algo_params.delta_rot) == float):
        algo_params.delta_rot = np.full_like(np.array(CompPerE), algo_params.delta_rot, dtype = float)
    elif (type(algo_params.delta_rot) == int):
        algo_params.delta_rot = np.full_like(np.array(CompPerE), algo_params.delta_rot, dtype = float)
    elif algo_params.delta_rot == None:
        algo_params.delta_rot = np.full(np.array(CompPerE).shape, algo_params.delta_rot, dtype = None)
    
    
    ReusedRef = False
    if algo_params.cube_ref is not None:    
        #To know the format used for cube_ref_delimiter
        if len(algo_params.cube_ref_delimiter) == 2*NEpochs:
            ReusedRef = True
        
    # We crop the PSF and check if PSF has been normalized (so that flux in
    # 1*FWHM aperture = 1) and fix if needed
    new_psf_size = int(round(3 * fwhm_med))
    if new_psf_size % 2 == 0:
        new_psf_size += 1

    if cube.ndim == 3:
        #Calculation of no_fc comparison
        Index = 0
        if algo_params.cube_ref is not None:                 

            for i in range(0, NEpochs, 1):
                StartIndexCube = algo_params.cube_delimiter[i]
                EndIndexCube = algo_params.cube_delimiter[i+1]
                
                if ReusedRef:
                    StartIndexCubeRef = algo_params.cube_ref_delimiter[2*i]
                    EndIndexCubeRef = algo_params.cube_ref_delimiter[2*i +1]
                
                else:
                    StartIndexCubeRef = algo_params.cube_ref_delimiter[i]
                    EndIndexCubeRef = algo_params.cube_ref_delimiter[i+1]
                
                
                for j in range(0, CompPerE[i], 1):
                    _, residuals_cube_, _ = pca_annular(
                        algo_params.cube[StartIndexCube:EndIndexCube,:,:],
                        algo_params.angle_list[StartIndexCube:EndIndexCube],
                        cube_ref = algo_params.cube_ref[StartIndexCubeRef:EndIndexCubeRef,:,:],
                        ncomp = int(ncomp[i][j]), full_output = True, 
                        delta_rot = algo_params.delta_rot[i],
                        **Args_left, **rot_options)
                    
                    CorrectDimRes = np.zeros((DefaultSizeE, SizeImage, SizeImage), dtype = float)
                    CorrectDimRes[0:SizeEpochs[i], :, :] = residuals_cube_
                    Res_no_fc[Index, :, :, :] = CorrectDimRes
                    Index += 1
        
        
        else:
            for i in range(0, NEpochs, 1):
                StartIndex = algo_params.cube_delimiter[i]
                EndIndex = algo_params.cube_delimiter[i+1]
                
                for j in range(0, CompPerE[i], 1):
                    _, residuals_cube_, _ = pca_annular(
                        algo_params.cube[StartIndex:EndIndex,:,:],
                        algo_params.angle_list[StartIndex:EndIndex],
                        ncomp = int(ncomp[i][j]), full_output = True, 
                        delta_rot = algo_params.delta_rot[i],
                        **Args_left, **rot_options)
                    
                    CorrectDimRes = np.zeros((DefaultSizeE, SizeImage, SizeImage), dtype = float)
                    CorrectDimRes[0:SizeEpochs[i], :, :] = residuals_cube_
                    Res_no_fc[Index, :, :, :] = CorrectDimRes
                    Index += 1
                    
        
        M = np.ones(NEpochs)
        for i in range(0, NEpochs, 1):
            for j in range(NEpochs-1, -1, -1):
                if j >= i:
                    M[i] *= CompPerE[j]
    
        Indices = np.zeros(NEpochs)

        k = 0
        for i in range(0, NCombinations, 1):

            k = 0
            for j in range(0, NEpochs, 1):
                if (i+1)%M[NEpochs-j-1] == 0:
                    k = j+1
            
            
            GlobalResiduals_no_fc = np.array([[[]]])
            Sum = 0
            for j in range(0, NEpochs, 1):
                if j == 0:
                    GlobalResiduals_no_fc = Res_no_fc[int(Indices[0]), 0:SizeEpochs[j]]
                else:
                    Sum += CompPerE[j-1]
                    GlobalResiduals_no_fc = np.vstack((GlobalResiduals_no_fc, Res_no_fc[int(Sum+Indices[j]), 0:SizeEpochs[j]]))
            
            
            frames_no_fc[i,:,:] = np.median(GlobalResiduals_no_fc, axis = 0)
            
            
            noise, res_level, vector_radd = noise_per_annulus(frames_no_fc[i],
                                                              separation=fwhm_med,
                                                              fwhm=fwhm_med,
                                                              wedge=wedge)


            vector_radd = vector_radd[inner_rad - 1:]
            noise = noise[inner_rad - 1:]
            res_level = res_level[inner_rad - 1:]
            if verbose:
                print("Measured annulus-wise noise in resulting frame")
                timing(start_time)
            
            
            noiseF[i] = noise[D-1]+((noise[D]-noise[D-1])/(vector_radd[D]-vector_radd[D-1]))*(distance*fwhm-vector_radd[D-1])
            res_levelF[i] = res_level[D-1]+((res_level[D]-res_level[D-1])/(vector_radd[D]-vector_radd[D-1]))*(distance*fwhm-vector_radd[D-1])
            
            
            Indices[NEpochs-k-1] += 1
            for j in range(0, k, 1):
                Indices[NEpochs-j-1] = 0
        
        
        
        
        n, y, x = array.shape
        psf_template = normalize_psf(
            psf_template,
            fwhm=fwhm,
            verbose=verbose,
            size=min(new_psf_size, psf_template.shape[1]),
        )

        # Initialize the fake companions
        angle_branch = angular_range / nbranch
        thruput_arr = np.zeros((nbranch, noise.shape[0]))
        fc_map_all = np.zeros((nbranch * fc_rad_sep, y, x))
        frame_fc_all = np.zeros((nbranch * fc_rad_sep, y, x))
        cy, cx = frame_center(array[0])

        # each branch is computed separately
        for br in range(nbranch):
            # each pattern is computed separately. For each one the companions
            # are separated by "fc_rad_sep * fwhm", interleaving the injections
            cube_fc = array.copy()
            # filling map with small numbers
            fc_map = np.ones_like(array[0]) * 1e-6
            fcy = 0
            fcx = 0
            flux = fc_snr * np.nanmean(noiseF)
            cube_fc = cube_inject_companions(
                cube_fc,
                psf_template,
                parangles,
                flux,
                rad_dists=rad_dist,
                theta=br * angle_branch + theta,
                nproc=nproc,
                imlib=imlib,
                interpolation=interpolation,
                verbose=False,
            )
            
            
            y = cy + rad_dist * \
                np.sin(np.deg2rad(br * angle_branch + theta))
            x = cx + rad_dist * \
                np.cos(np.deg2rad(br * angle_branch + theta))
            fc_map = frame_inject_companion(
                fc_map, psf_template, y, x, flux, imlib, interpolation
            )
            fcy = y
            fcx = x

            if verbose:
                msg2 = "Fake companions injected in branch {} "
                print(msg2.format(br + 1))
                timing(start_time)
            
            
            Index = 0
            if algo_params.cube_ref is not None:                 

                for i in range(0, NEpochs, 1):
                    StartIndexCube = algo_params.cube_delimiter[i]
                    EndIndexCube = algo_params.cube_delimiter[i+1]
                    
                    if ReusedRef:
                        StartIndexCubeRef = algo_params.cube_ref_delimiter[2*i]
                        EndIndexCubeRef = algo_params.cube_ref_delimiter[2*i +1]
                    
                    else:
                        StartIndexCubeRef = algo_params.cube_ref_delimiter[i]
                        EndIndexCubeRef = algo_params.cube_ref_delimiter[i+1]
                        
                    for j in range(0, CompPerE[i], 1):
                        _, residuals_cube_, _ = pca_annular(
                            cube_fc[StartIndexCube:EndIndexCube,:,:],
                            algo_params.angle_list[StartIndexCube:EndIndexCube],
                            cube_ref = algo_params.cube_ref[StartIndexCubeRef:EndIndexCubeRef,:,:],
                            ncomp = int(ncomp[i][j]), full_output = True, 
                            delta_rot = algo_params.delta_rot[i],
                            **Args_left, **rot_options)
                        
                        CorrectDimRes = np.zeros((DefaultSizeE, SizeImage, SizeImage), dtype = float)
                        CorrectDimRes[0:SizeEpochs[i], :, :] = residuals_cube_
                        Res_fc[Index, br, :, :, :] = CorrectDimRes
                        Index += 1
            
            
            else:
                for i in range(0, NEpochs, 1):
                    StartIndex = algo_params.cube_delimiter[i]
                    EndIndex = algo_params.cube_delimiter[i+1]
                    
                    for j in range(0, CompPerE[i], 1):
                        _, residuals_cube_, _ = pca_annular(
                            cube_fc[StartIndex:EndIndex,:,:],
                            algo_params.angle_list[StartIndex:EndIndex],
                            ncomp = int(ncomp[i][j]), full_output = True, 
                            delta_rot = algo_params.delta_rot[i],
                            **Args_left, **rot_options)
                        
                        CorrectDimRes = np.zeros((DefaultSizeE, SizeImage, SizeImage), dtype = float)
                        CorrectDimRes[0:SizeEpochs[i], :, :] = residuals_cube_
                        Res_fc[Index, br, :, :, :] = CorrectDimRes
                        Index += 1
                    
             
    
            M = np.ones(NEpochs)
            for i in range(0, NEpochs, 1):
                for j in range(NEpochs-1, -1, -1):
                    if j >= i:
                        M[i] *= CompPerE[j]
        
            Indices = np.zeros(NEpochs)

            k = 0
            for i in range(0, NCombinations, 1):
    
                k = 0
                for j in range(0, NEpochs, 1):
                    if (i+1)%M[NEpochs-j-1] == 0:
                        k = j+1
                
                
                GlobalResiduals_fc = np.array([[[]]])
                Sum = 0
                for j in range(0, NEpochs, 1):
                    if j == 0:
                        GlobalResiduals_fc = Res_fc[int(Indices[0]), br, 0:SizeEpochs[j]]
                    else:
                        Sum += CompPerE[j-1]
                        GlobalResiduals_fc = np.vstack((GlobalResiduals_fc, Res_fc[int(Sum+Indices[j]), br, 0:SizeEpochs[j]]))
                
                frame_fc = np.median(GlobalResiduals_fc, axis = 0)
                
                Indices[NEpochs-k-1] += 1
                for j in range(0, k, 1):
                    Indices[NEpochs-j-1] = 0
    
    
                injected_flux = apertureOne_flux(fc_map, fcy, fcx, fwhm_med)
                recovered_flux = apertureOne_flux(
                    (frame_fc - frames_no_fc[i]), fcy, fcx, fwhm_med)
                
                thruput = recovered_flux / injected_flux
                
                Throughput[i, br] = thruput
        
        
    rad_samp = rad_dist
    noise_samp = noiseF
    res_lev_samp = res_levelF           
                
    res_lev_samp = np.abs(res_lev_samp)

    noise_samp_sm = noise_samp
    res_lev_samp_sm = res_lev_samp
    
    
    for i in range(0, NCombinations, 1):
        Thru_Cont[i,0] = np.nanmean(Throughput[i,:])


    
    if isinstance(starphot, float) or isinstance(starphot, int):
        Thru_Cont[:,1] = (
            (sigma * noise_samp_sm + res_lev_samp_sm) / Thru_Cont[:,0]
        ) / starphot
    else:
        Thru_Cont[:,1] = (
            (sigma * noise_samp_sm + res_lev_samp_sm) / Thru_Cont[:,0]
        ) / np.median(starphot)
    
    return Thru_Cont


def contr_dist(
    cube,
    angle_list,
    psf_template,
    fwhm,
    distance,
    pxscale,
    starphot,
    algo,
    sigma=5,
    nbranch=1,
    theta=0,
    inner_rad=1,
    fc_rad_sep=3,
    noise_sep=1,
    wedge=(0, 360),
    fc_snr=20,
    student=True,
    transmission=None,
    dpi=vip_figdpi,
    debug=False,
    verbose=True,
    full_output=False,
    save_plot=None,
    object_name=None,
    frame_size=None,
    fix_y_lim=(),
    figsize=vip_figsize,
    algo_class=None,
    matrix_adi_ref=None,
    angle_adi_ref=None,
    **algo_dict,
):
    if cube.ndim != 3 and cube.ndim != 4:
        raise TypeError("The input array is not a 3d or 4d cube")
    if cube.ndim == 3 and (cube.shape[0] != angle_list.shape[0]):
        raise TypeError("Input parallactic angles vector has wrong length")
    if cube.ndim == 4 and (cube.shape[1] != angle_list.shape[0]):
        raise TypeError("Input parallactic angles vector has wrong length")
    if cube.ndim == 3 and psf_template.ndim != 2:
        raise TypeError("Template PSF is not a frame (for ADI case)")
    if cube.ndim == 4 and psf_template.ndim != 3:
        raise TypeError("Template PSF is not a cube (for ADI+IFS case)")
    if transmission is not None:
        if len(transmission) != 2 and len(transmission) != cube.shape[0] + 1:
            msg = "Wrong shape for transmission should be 2xn_rad or (nch+1) "
            msg += "x n_rad, instead of {}".format(transmission.shape)
            raise TypeError(msg)

    if isinstance(fwhm, (np.ndarray, list)):
        fwhm_med = np.median(fwhm)
    else:
        fwhm_med = fwhm

    if verbose:
        start_time = time_ini()
        if isinstance(starphot, float) or isinstance(starphot, int):
            msg0 = "ALGO : {}, FWHM = {}, # BRANCHES = {}, SIGMA = {},"
            msg0 += " STARPHOT = {}"
            print(msg0.format(algo.__name__, fwhm_med, nbranch, sigma, starphot))
        else:
            msg0 = "ALGO : {}, FWHM = {}, # BRANCHES = {}, SIGMA = {}"
            print(msg0.format(algo.__name__, fwhm_med, nbranch, sigma))
            
    if cube.ndim != 3 and cube.ndim != 4:
        raise TypeError("The input array is not a 3d or 4d cube")

    else:
        if psf_template.shape[1] % 2 == 0:
            raise ValueError("Only odd-sized PSF is accepted")
        if not hasattr(algo, "__call__"):
            raise TypeError("Parameter `algo` must be a callable function")
        if not isinstance(inner_rad, int):
            raise TypeError("inner_rad must be an integer")
        angular_range = wedge[1] - wedge[0]
        if nbranch > 1 and angular_range < 360:
            msg = "Only a single branch is allowed when working on a wedge"
            raise RuntimeError(msg)
        
    nproc = algo_dict.get("nproc", 1)
    imlib = algo_dict.get("imlib", "vip-fft")
    interpolation = algo_dict.get("interpolation", "lanczos4")
    scaling = algo_dict.get("scaling", None)
    ncomp = algo_dict.get("ncomp")
    
    if cube.ndim == 3:
        if isinstance(ncomp, list):
            nnpcs = len(ncomp)
        elif isinstance(ncomp, tuple):
            if isinstance(ncomp[1], list):
                nnpcs = len(ncomp[1])
            else:
                nnpcs = 1
        else:
            nnpcs = 1
    elif cube.ndim == 4:
        if isinstance(ncomp, list):
            if len(ncomp) == cube.shape[0]:
                nnpcs = 1
            else:
                nnpcs = len(ncomp)
        else:
            nnpcs = 1
        
    SizeImage = int(cube[0].shape[0])
    frames_fc = np.zeros((nnpcs, nbranch, SizeImage, SizeImage), dtype = float)
    frames_no_fc = np.zeros((nnpcs, SizeImage, SizeImage), dtype = float)
    
    rad_dist = distance * fwhm
    
    if isinstance(fwhm, (np.ndarray, list)):
        fwhm_med = np.median(fwhm)
    else:
        fwhm_med = fwhm

    if verbose:
        start_time = time_ini()
        
    algo_name = algo.__name__
    idx = algo.__module__.index('.', algo.__module__.index('.') + 1)
    mod = algo.__module__[:idx]
    tmp = __import__(mod, fromlist=[algo_name.upper()+'_Params'])    
    algo_params = getattr(tmp, algo_name.upper()+'_Params')
    
    if matrix_adi_ref is not None:
        if 'cube_ref' in algo_dict.keys() and algo_dict['cube_ref'] is not None:
            NAdiRef = algo_dict['cube_ref'].shape[0]
            algo_dict['cube_ref'] = np.vstack((algo_dict['cube_ref'], matrix_adi_ref))
        else:
            NAdiRef = 0
            algo_dict['cube_ref'] = matrix_adi_ref
        NRefT = algo_dict['cube_ref'].shape[0]
    
    frames_no_fc[:, :, :] = np.array(algo(cube=cube, angle_list=angle_list, fwhm=fwhm_med,
                      verbose=verbose, **algo_dict))

    
    #CHANGE NOISE ANNULI TO HAVE IT ONLY HERE AT THIS DISTANCE !!!
    noise_res = [noise_dist(frames_no_fc[i], rad_dist, fwhm_med, wedge, 
                                  False, debug) for i in range(0, nnpcs)]
    
    noise = np.array(noise_res)[:, 0]
    mean_res = np.array(noise_res)[:, 1]
    
    # We crop the PSF and check if PSF has been normalized (so that flux in
    # 1*FWHM aperture = 1) and fix if needed
    new_psf_size = int(round(3 * fwhm_med))
    if new_psf_size % 2 == 0:
        new_psf_size += 1
    
    n, y, x = cube.shape
    psf_template = normalize_psf(
        psf_template,
        fwhm=fwhm,
        verbose=verbose,
        size=min(new_psf_size, psf_template.shape[1]),
    )
    
    
    # Initialize the fake companions
    angle_branch = angular_range / nbranch
    Throughput = np.zeros((nnpcs, nbranch))
    fc_map = np.zeros((nbranch, y, x))
    cy, cx = frame_center(cube[0])
    parangles = angle_list

    # each branch is computed separately
    if matrix_adi_ref is not None:
        copy_ref = np.copy(algo_dict['cube_ref'])
        
    for br in range(nbranch):
        
        if matrix_adi_ref is not None:
            algo_dict['cube_ref'] = np.copy(copy_ref)
        
        # each pattern is computed separately. For each one the companions
        # are separated by "fc_rad_sep * fwhm", interleaving the injections
        fc_map = np.ones_like(cube[0]) * 1e-6
        fcy = 0
        fcx = 0
        flux = fc_snr * np.min(noise)
        
        if matrix_adi_ref is None:
            cube_fc = cube.copy()
        else:
            cube_fc = cube.copy()
            cube_adi_fc = np.copy(algo_dict['cube_ref'][NAdiRef:NRefT, :, :])
            cube_fc = np.vstack((cube_fc, cube_adi_fc))
            parangles = np.concatenate((angle_list, angle_adi_ref))
        
        cube_fc = cube_inject_companions(
            cube_fc,
            psf_template,
            parangles,
            flux,
            rad_dists=rad_dist,
            theta=br * angle_branch + theta,
            nproc=nproc,
            imlib=imlib,
            interpolation=interpolation,
            verbose=False,
            )
        
        if matrix_adi_ref is not None:
            algo_dict['cube_ref'][NAdiRef:NRefT, :, :] = cube_fc[n:, :, :]
            cube_fc = cube_fc[0:n, :, :]
            
        
        y = cy + rad_dist * \
            np.sin(np.deg2rad(br * angle_branch + theta))
        x = cx + rad_dist * \
            np.cos(np.deg2rad(br * angle_branch + theta))
        fc_map = frame_inject_companion(
            fc_map, psf_template, y, x, flux, imlib, interpolation
        )
        fcy = y
        fcx = x

        if verbose:
            msg2 = "Fake companions injected in branch {} "
            print(msg2.format(br + 1))
            timing(start_time)
    
        frames_fc[:, br, :, :] = algo(cube=cube_fc, angle_list=angle_list, 
                                      fwhm=fwhm_med, verbose=verbose, **algo_dict)
        
        injected_flux = apertureOne_flux(fc_map, fcy, fcx, fwhm_med)
        recovered_flux = np.array([apertureOne_flux(
            (frames_fc[i, br, :, :] - frames_no_fc[i, :, :]), fcy, fcx, fwhm_med
        ) for i in range(0, nnpcs)])
        
        thruput = recovered_flux / injected_flux
        thruput[np.where(thruput < 0)] = 0
        
        Throughput[:, br] = thruput.reshape((nnpcs))
        

    noise_samp = noise
    res_lev_samp = mean_res         
                
    res_lev_samp = np.abs(res_lev_samp)

    noise_samp_sm = noise_samp
    res_lev_samp_sm = res_lev_samp
    
    
    Thru_Cont = np.zeros((nnpcs, 3))
    if isinstance(ncomp, tuple):
        Thru_Cont[:, 2] = ncomp[1]
    else:
        Thru_Cont[:, 2] = ncomp
    
    Thru_Cont[:,0] = [np.nanmean(Throughput[i,:]) for i in range(0, nnpcs)]

    
    if isinstance(starphot, float) or isinstance(starphot, int):
        Thru_Cont[:,1] = (
            (sigma * noise_samp_sm + res_lev_samp_sm) / Thru_Cont[:,0]
        ) / starphot
    else:
        Thru_Cont[:,1] = (
            (sigma * noise_samp_sm + res_lev_samp_sm) / Thru_Cont[:,0]
        ) / np.median(starphot)
        
    return (Thru_Cont, frames_fc)



def contrast_step_pca_ann(
    cube,
    angle_list,
    psf_template,
    fwhm,
    distance,
    pxscale,
    starphot,
    algo,
    step,
    through_thresh=0.1,
    sigma=5,
    nbranch=1,
    theta=0,
    inner_rad=1,
    fc_rad_sep=3,
    noise_sep=1,
    wedge=(0, 360),
    fc_snr=20,
    student=True,
    transmission=None,
    dpi=vip_figdpi,
    debug=False,
    verbose=True,
    full_output=False,
    save_plot=None,
    object_name=None,
    frame_size=None,
    fix_y_lim=(),
    figsize=vip_figsize,
    algo_class=None,
    matrix_adi_ref=None,
    angle_adi_ref=None,
    **algo_dict,
):
    if cube.ndim != 3 and cube.ndim != 4:
        raise TypeError("The input array is not a 3d or 4d cube")
    if cube.ndim == 3 and (cube.shape[0] != angle_list.shape[0]):
        raise TypeError("Input parallactic angles vector has wrong length")
    if cube.ndim == 4 and (cube.shape[1] != angle_list.shape[0]):
        raise TypeError("Input parallactic angles vector has wrong length")
    if cube.ndim == 3 and psf_template.ndim != 2:
        raise TypeError("Template PSF is not a frame (for ADI case)")
    if cube.ndim == 4 and psf_template.ndim != 3:
        raise TypeError("Template PSF is not a cube (for ADI+IFS case)")
    if transmission is not None:
        if len(transmission) != 2 and len(transmission) != cube.shape[0] + 1:
            msg = "Wrong shape for transmission should be 2xn_rad or (nch+1) "
            msg += "x n_rad, instead of {}".format(transmission.shape)
            raise TypeError(msg)

    if isinstance(fwhm, (np.ndarray, list)):
        fwhm_med = np.median(fwhm)
    else:
        fwhm_med = fwhm

    if verbose:
        start_time = time_ini()
        if isinstance(starphot, float) or isinstance(starphot, int):
            msg0 = "ALGO : {}, FWHM = {}, # BRANCHES = {}, SIGMA = {},"
            msg0 += " STARPHOT = {}"
            print(msg0.format(algo.__name__, fwhm_med, nbranch, sigma, starphot))
        else:
            msg0 = "ALGO : {}, FWHM = {}, # BRANCHES = {}, SIGMA = {}"
            print(msg0.format(algo.__name__, fwhm_med, nbranch, sigma))
            
    if cube.ndim != 3 and cube.ndim != 4:
        raise TypeError("The input array is not a 3d or 4d cube")

    else:
        if psf_template.shape[1] % 2 == 0:
            raise ValueError("Only odd-sized PSF is accepted")
        if not hasattr(algo, "__call__"):
            raise TypeError("Parameter `algo` must be a callable function")
        if not isinstance(inner_rad, int):
            raise TypeError("inner_rad must be an integer")
        angular_range = wedge[1] - wedge[0]
        if nbranch > 1 and angular_range < 360:
            msg = "Only a single branch is allowed when working on a wedge"
            raise RuntimeError(msg)
        
    nproc = algo_dict.get("nproc", 1)
    imlib = algo_dict.get("imlib", "vip-fft")
    interpolation = algo_dict.get("interpolation", "lanczos4")
    scaling = algo_dict.get("scaling", None)
    ncomp = algo_dict.get("ncomp")
    
    if cube.ndim == 3:
        if isinstance(ncomp, list):
            nnpcs = len(ncomp)
        elif isinstance(ncomp, tuple):
            if isinstance(ncomp[1], list):
                nnpcs = len(ncomp[1])
            else:
                nnpcs = 1
        else:
            nnpcs = 1
    elif cube.ndim == 4:
        if isinstance(ncomp, list):
            if len(ncomp) == cube.shape[0]:
                nnpcs = 1
            else:
                nnpcs = len(ncomp)
        else:
            nnpcs = 1
        
    SizeImage = int(cube[0].shape[1])
    NbrImages = int(cube.shape[0])
    if NbrImages % step != 0:
        raise ValueError("Number of images must be divisible by the step")
    TotalSteps = int(NbrImages/step)
    
    frames_fc = np.zeros((nnpcs, nbranch, SizeImage, SizeImage), dtype = float)
    frames_no_fc = np.zeros((nnpcs, SizeImage, SizeImage), dtype = float)
    res_cube_fc = np.zeros((nnpcs, nbranch, NbrImages, SizeImage, SizeImage), dtype = float)
    res_cube_no_fc = np.zeros((nnpcs, NbrImages, SizeImage, SizeImage), dtype = float)
    
    rad_dist = distance * fwhm
    
    if isinstance(fwhm, (np.ndarray, list)):
        fwhm_med = np.median(fwhm)
    else:
        fwhm_med = fwhm

    if verbose:
        start_time = time_ini()
        
    algo_name = algo.__name__
    idx = algo.__module__.index('.', algo.__module__.index('.') + 1)
    mod = algo.__module__[:idx]
    tmp = __import__(mod, fromlist=[algo_name.upper()+'_Params'])    
    algo_params = getattr(tmp, algo_name.upper()+'_Params')
    
    if matrix_adi_ref is not None:
        if 'cube_ref' in algo_dict.keys() and algo_dict['cube_ref'] is not None:
            NAdiRef = algo_dict['cube_ref'].shape[0]
            algo_dict['cube_ref'] = np.vstack((algo_dict['cube_ref'], matrix_adi_ref))
        else:
            NAdiRef = 0
            algo_dict['cube_ref'] = matrix_adi_ref
        NRefT = algo_dict['cube_ref'].shape[0]
    
    if algo_name == "pca_annular":
        _, res_cube_no_fc[:, :, :, :], frames_no_fc[:, :, :] = algo(
                        cube=cube, angle_list=angle_list, fwhm=fwhm_med,
                        verbose=verbose, full_output = True, **algo_dict)
    else:
        raise ValueError("Algorithm not supported")

    
    #CHANGE NOISE ANNULI TO HAVE IT ONLY HERE AT THIS DISTANCE !!!
    noise_res = []
    noise = []
    mean_res = []
    noise_avg = [noise_dist(frames_no_fc[i, :, :], rad_dist, fwhm_med, wedge, 
                        False, debug) for i in range(0, nnpcs)]
    for s in range(0, TotalSteps, 1):
        noise_res.append(np.array(
            [noise_dist(np.median(res_cube_no_fc[i, s*step:(s+1)*step:1, :, :], axis = 0), 
            rad_dist, fwhm_med, wedge, False, debug) for i in range(0, nnpcs)]))
        noise.append(np.array(noise_res[s])[:, 0])
        mean_res.append(np.array(noise_res[s])[:, 1])
    
    
    # We crop the PSF and check if PSF has been normalized (so that flux in
    # 1*FWHM aperture = 1) and fix if needed
    new_psf_size = int(round(3 * fwhm_med))
    if new_psf_size % 2 == 0:
        new_psf_size += 1
    
    n, y, x = cube.shape
    psf_template = normalize_psf(
        psf_template,
        fwhm=fwhm,
        verbose=verbose,
        size=min(new_psf_size, psf_template.shape[1]),
    )
    
    
    # Initialize the fake companions
    angle_branch = angular_range / nbranch
    Throughput = np.zeros((TotalSteps, nnpcs, nbranch))
    fc_map = np.zeros((nbranch, y, x))
    cy, cx = frame_center(cube[0])
    parangles = angle_list

    # each branch is computed separately
    if matrix_adi_ref is not None:
        copy_ref = np.copy(algo_dict['cube_ref'])
        
    loc = []
    for br in range(nbranch):
        
        if matrix_adi_ref is not None:
            algo_dict['cube_ref'] = np.copy(copy_ref)
        
        # each pattern is computed separately. For each one the companions
        # are separated by "fc_rad_sep * fwhm", interleaving the injections
        fc_map = np.ones_like(cube[0]) * 1e-6
        fcy = 0
        fcx = 0
        flux = fc_snr * np.min(np.array(noise_avg)[:, 0])
        
        if matrix_adi_ref is None:
            cube_fc = cube.copy()
        else:
            cube_fc = cube.copy()
            cube_adi_fc = np.copy(algo_dict['cube_ref'][NAdiRef:NRefT, :, :])
            cube_fc = np.vstack((cube_fc, cube_adi_fc))
            parangles = np.concatenate((angle_list, angle_adi_ref))
        
        cube_fc = cube_inject_companions(
            cube_fc,
            psf_template,
            parangles,
            flux,
            rad_dists=rad_dist,
            theta=br * angle_branch + theta,
            nproc=nproc,
            imlib=imlib,
            interpolation=interpolation,
            verbose=False,
            )
        
        if matrix_adi_ref is not None:
            algo_dict['cube_ref'][NAdiRef:NRefT, :, :] = cube_fc[n:, :, :]
            cube_fc = cube_fc[0:n, :, :]
            
        
        y = cy + rad_dist * \
            np.sin(np.deg2rad(br * angle_branch + theta))
        x = cx + rad_dist * \
            np.cos(np.deg2rad(br * angle_branch + theta))
        fc_map = frame_inject_companion(
            fc_map, psf_template, y, x, flux, imlib, interpolation
        )
        fcy = y
        fcx = x
        loc.append((fcy, fcx))

        if verbose:
            msg2 = "Fake companions injected in branch {} "
            print(msg2.format(br + 1))
            timing(start_time)
    
        _, res_cube_fc[:, br, : ,:, :], frames_fc[:, br, :, :] = algo(cube=cube_fc, 
                    angle_list=angle_list, fwhm=fwhm_med, verbose=verbose, 
                    full_output = True, **algo_dict)
        
        injected_flux = apertureOne_flux(fc_map, fcy, fcx, fwhm_med)
        recovered_flux_avg = np.array([apertureOne_flux(
            (frames_fc[i, br, :, :] - frames_no_fc[i, :, :]), fcy, fcx, fwhm_med
        ) for i in range(0, nnpcs)])
        
        recovered_flux = []
        thruput = []
        for s in range(0, TotalSteps, 1):
            frame_step_no_fc = np.median(res_cube_no_fc[:, s*step:(s+1)*step, :, :], axis = 1)
            frame_step_fc = np.median(res_cube_fc[:, br, s*step:(s+1)*step, :, :], axis = 1)
            recovered_flux.append(np.array([apertureOne_flux(
                (frame_step_fc[i, :, :] - frame_step_no_fc[i, :, :]), fcy, fcx, fwhm_med
            ) for i in range(0, nnpcs)]))
            thruput.append(np.array(recovered_flux[s] / injected_flux))
            thruput[s][np.where(thruput[s] < 0)] = 0
            Throughput[s, :, br] = thruput[s].reshape((nnpcs))
        

    noise_samp_sm = noise               
    res_lev_samp_sm = np.abs(mean_res)
    
    BestNInd = np.zeros((TotalSteps), dtype=int)
    
    Thru_Cont = np.zeros((TotalSteps, nnpcs, 2))
    
    for s in range(0, TotalSteps, 1):
        Thru_Cont[s,:,0] = [np.nanmean(Throughput[s,i,:]) for i in range(0, nnpcs)]
        if isinstance(starphot, float) or isinstance(starphot, int):
            Thru_Cont[s,:,1] = (
                (sigma * noise_samp_sm[s] + res_lev_samp_sm[s]) / Thru_Cont[s,:,0]
            ) / starphot
        else:
            Thru_Cont[s,:,1] = (
                (sigma * noise_samp_sm[s] + res_lev_samp_sm[s]) / Thru_Cont[s,:,0]
            ) / np.median(starphot)

    #SELECT BEST COMP FOR EACH STEP
    #THRESHOLD ON THROUGHPUT + if all contr = infinite at that step, take avg...
    for s in range(0, TotalSteps, 1):
        indices = np.where(Thru_Cont[s, :, 0] >= through_thresh)[0]
        for i in range(0, nnpcs, 1):
            if Thru_Cont[s, i, 1] != np.inf:
                break
            BestNInd[s] = -1
            continue
        if indices.shape[0] == 0:
            BestNInd[s] = -1
            continue
        Min_ind = indices[np.argmin(Thru_Cont[s, indices, 1])]
        BestNInd[s] = Min_ind
    
    
    ncomp = np.array(ncomp)
    AvgN = int(np.mean(BestNInd[np.where(BestNInd != -1)]))
    #AvgN = int(np.nanmean(BestNInd))
    BestNInd[np.where(BestNInd == -1)[0]] = AvgN
    BestComp = ncomp[BestNInd]
    
    final_no_fc = np.zeros((NbrImages, SizeImage, SizeImage), dtype = float)
    final_fc = np.zeros((nbranch, NbrImages, SizeImage, SizeImage), dtype = float)
    for s in range(0, TotalSteps, 1):
        final_no_fc[s*step:(s+1)*step,:,:] = res_cube_no_fc[BestNInd[s],s*step:(s+1)*step,:,:]
        for br in range(0, nbranch):
            final_fc[br,s*step:(s+1)*step,:,:] = res_cube_fc[BestNInd[s],br,s*step:(s+1)*step,:,:]
    
    final_frame = np.median(final_no_fc, axis = 0)
    final_frames_br = np.array([np.median(final_fc[br,:,:,:], axis = 0) 
                                            for br in range(0, nbranch)])
    
    final_noise, final_mean_res = noise_dist(final_frame, 
                                    rad_dist, fwhm_med, wedge, False, debug)
    
    final_recovered_fluxes = np.array([apertureOne_flux(
        (final_frames_br[br, :, :] - final_frame), loc[br][0], loc[br][1], fwhm_med) 
                                            for br in range(0, nbranch)])
    
    final_thruput = np.array(final_recovered_fluxes / injected_flux)
    final_thruput[np.where(final_thruput < 0)] = 0
    final_result = np.zeros((2))
    final_result[0] = np.nanmean(final_thruput)
    
    if isinstance(starphot, float) or isinstance(starphot, int):
        final_result[1] = (
            (sigma * final_noise + final_mean_res) / final_result[0]
        ) / starphot
    else:
        final_result[1] = (
            (sigma * final_noise + final_mean_res) / final_result[0]
        ) / np.median(starphot)
        
    return (BestComp, final_result, final_frame, final_frames_br, frames_fc)





def noise_dist(array, distance, fwhm, wedge=(0, 360), verbose=False, debug=False):
    """
    distance is the distance at which noise level is evaluated
    ATTENTION: in noise per annulus function, separation is the separation 
    between each annuli. Init_rad is the initial distance
    of the first annuli, annuli whose width is equal to separation then...
    
    sep in find_coords is the "angular" separation between each aperture used
    for the evaluation of the noise

    """
    def find_coords(rad, sep, init_angle, fin_angle):
        angular_range = fin_angle - init_angle
        npoints = (np.deg2rad(angular_range) * rad) / sep  # (2*np.pi*rad)/sep
        ang_step = angular_range / npoints  # 360/npoints
        x = []
        y = []
        for i in range(int(npoints)):
            newx = rad * np.cos(np.deg2rad(ang_step * i + init_angle))
            newy = rad * np.sin(np.deg2rad(ang_step * i + init_angle))
            x.append(newx)
            y.append(newy)
        return np.array(y), np.array(x)
    
    init_angle, fin_angle = wedge
    centery, centerx = frame_center(array)
    
    if debug:
        _, ax = plt.subplots(figsize=(6, 6))
        ax.imshow(
            array, origin="lower", interpolation="nearest", alpha=0.5, cmap="gray"
        )

    y = centery + distance
    rad = dist(centery, centerx, y, centerx)
    yy, xx = find_coords(rad, fwhm, init_angle, fin_angle)
    yy += centery
    xx += centerx

    apertures = CircularAperture(np.array((xx, yy)).T, fwhm / 2)
    fluxes = aperture_photometry(array, apertures)
    fluxes = np.array(fluxes["aperture_sum"])

    noise_dist = np.std(fluxes)
    mean_dist = np.mean(fluxes)

    if debug:
        for j in range(xx.shape[0]):
            # Circle takes coordinates as (X,Y)
            aper = plt.Circle(
                (xx[j], yy[j]), radius=fwhm / 2, color="r", fill=False, alpha=0.8
            )
            ax.add_patch(aper)
            cent = plt.Circle(
                (xx[j], yy[j]), radius=0.8, color="r", fill=True, alpha=0.5
            )
            ax.add_patch(cent)

    if verbose:
        print("Radius(px) = {}, Noise = {:.3f} ".format(rad, noise_dist))

    return noise_dist, mean_dist


def apertureOne_flux(array, yc, xc, fwhm, ap_factor=1, mean=False, verbose=False):
    """Returns the sum of pixel values in a circular aperture centered on the
    input coordinates. The radius of the aperture is set as (ap_factor*fwhm)/2.

    Parameters
    ----------
    array : numpy ndarray
        Input frame.
    yc, xc : y and x coordinates of sources.
    fwhm : float
        FWHM in pixels.
    ap_factor : int, optional
        Diameter of aperture in terms of the FWHM.

    Returns
    -------
    flux : list of floats
        List of fluxes.

    Note
    ----
    From Photutils documentation, the aperture photometry defines the aperture
    using one of 3 methods:

    'center': A pixel is considered to be entirely in or out of the aperture
              depending on whether its center is in or out of the aperture.
    'subpixel': A pixel is divided into subpixels and the center of each
                subpixel is tested (as above).
    'exact': (default) The exact overlap between the aperture and each pixel is
             calculated.

    """
    flux = 0
    if mean:
        ind = disk((yc, xc), (ap_factor * fwhm) / 2)
        values = array[ind]
        obj_flux = np.mean(values)
    else:
        aper = CircularAperture((xc, yc), (ap_factor * fwhm) / 2)
        obj_flux = aperture_photometry(array, aper, method="exact")
        obj_flux = np.array(obj_flux["aperture_sum"])
        
    flux = obj_flux

    return flux