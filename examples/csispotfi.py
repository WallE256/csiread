#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""SpotFi: `SpotFi: Decimeter Level Localization Using WiFi`

Usage:
    python3 csispotfi.py

Note:
    This example was tested with `spotfimusicaoaestimation/sample_csi_trace.mat`

Important:
    1. This example can get the same result as `spotfimusicaoaestimation`, but it
    hasn't been tested with WiFi Indoor Positioning Dataset. There may be some
    issues.
    2. Unfinished: 'Cluster AoA and ToF from multiple packets' hasn't been
    implemented yet.

Ref:
    1. [SpotFi: Decimeter Level Localization Using WiFi](http://conferences.sigcomm.org/sigcomm/2015/pdf/papers/p269.pdf)
    2. [spotfiMusicAoaEstimation](https://bitbucket.org/mkotaru/spotfimusicaoaestimation)
"""

from math import floor

import csiread
import matplotlib.pyplot as plt
import numpy as np
from numpy.linalg import eigh, norm
from scipy.io import loadmat
from scipy.linalg import hankel
from scipy.stats import gmean
from skimage.feature.peak import peak_local_max

from utils import db, scidx


RANGE_TOF_START, RANGE_TOF_STOP, RANGE_TOF_STEP = [-25, 25, 101]
RANGE_DOA_START, RANGE_DOA_STOP, RANGE_DOA_STEP = [-np.pi / 2, np.pi / 2, 101]


def loadcsi(file):
    """Load CSI Tensor

    Args:
        file (str): csi data file

    Returns:
        ndarray: csi, shape=[count, subcarriers, nrx]
    """
    if file.endswith('.mat'):
        csi = loadmat(file)['sample_csi_trace']
        csi = csi.reshape(1, 3, 30).transpose(0, 2, 1)
    else:
        csidata = csiread.Intel(file, if_report=False)
        csidata.read()
        csi = csidata.get_scaled_csi_sm(True)[..., 0]
    return csi


def remove_sto(csi, bw=20, ng=2):
    """Algorithm 1: SpotFi’s ToF sanitization algorithm

    Args:
        csi: shape=[count, subcarriers, nrx]
        bw: bandwitdh(20, 40)
        ng: grouping(1, 2, 4)

    Ref:
        spotfiMusicAoaEstimation: removePhsSlope.m
    """
    count, s_num = csi.shape[0], csi.shape[1]
    s = scidx(bw, ng)[:, np.newaxis]
    x = np.tile(s.ravel(), 3)

    # Unwrap phase
    phase = np.unwrap(np.angle(csi), axis=1)
    # Optional ? Maybe
    umask = phase[:, :1, :] - phase[:, :1, :1] > np.pi
    lmask = phase[:, :1, :] - phase[:, :1, :1] < -np.pi
    umask = np.repeat(umask, s_num, 1)
    lmask = np.repeat(lmask, s_num, 1)
    phase[umask] -= 2 * np.pi
    phase[lmask] += 2 * np.pi
 
    # In the paper, step 1 is least-squares 
    a = np.c_[x, np.ones_like(x)]
    b = phase.T.reshape(-1, count)
    m, c = np.linalg.lstsq(a, b, rcond=None)[0]
    m = m[:, np.newaxis, np.newaxis]
    c = c[:, np.newaxis, np.newaxis]

    return csi * np.exp(-1.j * (m * s + c))


def hankel2(csi):
    """Nested Hankel matrix for csi

    Args:
        csi: shappe=[subcarriers, nrx]

    Returns:
        ndarray:, h2, shape=[L * Ln, J * Jn]
    """
    S, N = csi.shape
    L, Ln = floor(S / 2), floor(N / 2) + 1
    J, Jn = S - L + 1, N - Ln + 1

    # Level 1: subcarriers
    h = [hankel(csi[:L, i], csi[-J:, i]) for i in range(N)]
    h = np.asarray(h)       # h.shape=[nrx, L, J]

    # Level 2: nrx
    h2 = np.empty([L * Ln, J * Jn], csi.dtype)
    for j in range(Jn):
        h2[:, j*J:j*J+J] = h[j:j+Ln].reshape(-1, J)
    return h2


def smooth_csi(csi):
    """Fig.4: CSI smoothing

    Args:
        csi: shape=[count, subcarriers, nrx]

    Returns:
        ndarray: smoothed_csi, e.g. shape=[1, 30, 32]
    """
    return np.asarray([hankel2(i) for i in csi])


def NoSS_old(X, EigDiffCutoff=4):
    """Older way of finding the SignalEndIdx based on thresholding and
    eigenvalue difference cutoff

    Args:
        X: CSI, shape=[count, subcarriers, nrx]
        EigDiffCutoff: eigenvalue difference cutoff

    Refs:
        1. GetQnBackscatter.m: line 50-64
    """
    T, M = X.shape[2], X.shape[1]
    R = (X @ X.transpose(0, 2, 1).conj()).mean(axis=0)
    w, v = eigh(R)
    w = sorted(np.abs(w), reverse=True)[:5]

    Criterion1 = np.diff(db(w)) <= max(-EigDiffCutoff, min(db(w)))
    Criterion3 = w[:-1] / w[0] > 0.03
    index, = np.nonzero(Criterion1 * Criterion3)
    return index[-1] + 1


def NoSS(X, method='MDL'):
    """Number of Signal Source"""
    T, M = X.shape[2], X.shape[1]
    R = (X @ X.transpose(0, 2, 1).conj()).mean(axis=0)
    w, v = eigh(R)
    w = sorted(np.abs(w))

    def Lambda(n):
        return np.mean(w[:-n]) / gmean(w[:-n])

    def AIC(n):
        return 2 * T * (M - n) * np.log(Lambda(n)) + 2 * n * (2 * M - n)

    def MDL(n):
        return T * (M - n) * np.log(Lambda(n)) + 1/2 * n * (2 * M - n) * np.log(T)

    def HQ(n):
        return T * (M - n) * np.log(Lambda(n)) + 1/2 * n * (2 * M - n) * np.log(np.log(T))

    func = eval(method)
    D = np.argmin([func(i) for i in range(1, M)]) + 1
    return D


def music(X, D, nrx, d, f, bw, ng, c=3e8):
    """MUSIC for SpotFi

    Args:
        X: smoothed csi, shape=[count, L * Ln, J * Jn]
        D: number of sources
        nrx: number receiving antennas
        d: distance between receiving antennas, (m)
        f: center frequency, (Hz)
        c: wave speed, (m/s)
        bw: bandwitdh(20, 40), (MHz)
        ng: grouping(1, 2, 4)
    """
    T, M = X.shape[2], X.shape[1]
    Ln = floor(nrx / 2) + 1
    L = M // Ln
    S_INDEX = scidx(bw, ng)[:, np.newaxis]
    DELTA_K = 3.125e5           # Hz

    R = (X @ X.transpose(0, 2, 1).conj()).mean(axis=0)
    w, v = eigh(R)   # w may contain negative values
    idx = np.abs(w).argsort()[::-1]
    w, v = w[idx], v[:, idx]
    u, s = v[:, D:], v[:, :D]

    # ULA
    ula = np.c_[:Ln] * d
    # tof search space, (s)
    tof_space = np.linspace(RANGE_TOF_START, RANGE_TOF_STOP, RANGE_TOF_STEP) * 1e-9
    # doa search space, (rad)
    doa_space = np.linspace(RANGE_DOA_START, RANGE_DOA_STOP, RANGE_DOA_STEP)

    # steering vector
    a_tof = np.exp(-2.j * np.pi * S_INDEX[:L] * DELTA_K * tof_space)
    a_doa = np.exp(-2.j * np.pi * f * ula * np.sin(doa_space) / c)
    a = np.kron(a_doa, a_tof)

    # spotfimusicaoaestimation get the MUSIC pseudospectrum with signal subspace

    # p = norm(s.T.conj() @ a, axis=0) / norm(a, axis=0)
    p = norm(a, axis=0) / norm(u.T.conj() @ a, axis=0)
    p = p.reshape(doa_space.size, tof_space.size)
    p = 10 * np.log10(p/p.max())

    # Tips: RAPMusicGridMaxBackscatter.m finds a peak by maximum function.
    # It finds all peaks by maximum suppression and Multiple iterations

    pos_doa, pos_tof = peak_local_max(p, num_peaks=D).T
    doa = doa_space[pos_doa]
    tof = tof_space[pos_tof]

    return p, doa, tof


def plotspectrum(p, doa, tof):
    tof_space = np.linspace(RANGE_TOF_START, RANGE_TOF_STOP, p.shape[1])    # (ns)
    doa_space = np.linspace(RANGE_DOA_START, RANGE_DOA_STOP, p.shape[0])
    doa_space = np.rad2deg(doa_space)                                       # (degree)
    doa = np.rad2deg(doa)
    tof = tof * 1e9
    print('Doa: ', doa)
    print('Tof: ', tof)

    fig = plt.figure(figsize=(16, 9))

    ax1 = fig.add_subplot(221)
    ax1.set_title('MUSIC-2D')
    ax1.set_xlabel('doa [deg]')
    ax1.set_ylabel('tof [ns]')
    ax1.pcolormesh(doa_space, tof_space, p.T, shading='gouraud',
                   cmap='jet', antialiased=True)
    ax1.plot(doa, tof, 'o', color='black', markersize=10, alpha=0.5)

    ax2 = fig.add_subplot(222, sharey=ax1)
    ax2.set_title('MUSIC-tof')
    ax2.set_xlabel('Power [dB]')
    ax2.set_ylabel('tof [ns]')
    ax2.plot(p.max(0), tof_space)
    ax2.hlines(tof, p.min(), p.max(),
               colors='orange', linewidth=2, linestyles='--')

    ax3 = fig.add_subplot(223, sharex=ax1)
    ax3.set_title('MUSIC-doa')
    ax3.set_xlabel('doa [deg]')
    ax3.set_ylabel('Power [dB]')
    ax3.plot(doa_space, p.max(1))
    ax3.vlines(doa, p.min(), p.max(),
               colors='orange', linewidth=2, linestyles='--')

    ax4 = fig.add_subplot(224, projection='3d')
    ax4.view_init(elev=45., azim=255)
    ax4.set_title('MUSIC-3D')
    ax4.set_xlabel('doa [deg]')
    ax4.set_ylabel('tof [ns]')
    ax4.set_zlabel('Power [dB]')
    x = np.arange(len(doa_space))
    y = np.arange(len(tof_space))
    x, y = np.meshgrid(x, y)
    ax4.plot_surface(doa_space[x], tof_space[y], p[x, y],
                     cmap='jet', antialiased=True)

    plt.tight_layout()
    plt.show()


def spotfi(csi, d, f, bw, ng):
    """SpotFi"""
    nrx = csi.shape[-1]
    csi = remove_sto(csi, bw, ng)
    csi = smooth_csi(csi)
    p, doa, tof = music(X=csi, D=NoSS_old(csi), nrx=nrx, d=d, f=f, bw=bw, ng=ng)
    plotspectrum(p, doa, tof)


if __name__ == '__main__':
    # csi = loadcsi('spotfimusicaoaestimation/sample_csi_trace.mat')
    # spotfi(csi, d=2.6e-2, f=5.63e9, bw=40, ng=4)

    # d is unkown and 2.82e-2 is a random value. The following section is just a demo.
    RANGE_TOF_START, RANGE_TOF_STOP, RANGE_TOF_STEP = [-50, 150, 201]
    csi = loadcsi('../material/5300/dataset/sample_0x5_64_3000.dat')[1000:1002]
    spotfi(csi, d=2.82e-2, f=5.32e9, bw=20, ng=2)
