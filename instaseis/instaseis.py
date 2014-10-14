#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Python interface to an AxiSEM database in a netCDF file.

:copyright:
    Martin van Driel (Martin@vanDriel.de), 2014
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2014
:license:
    GNU General Public License, Version 3
    (http://www.gnu.org/copyleft/gpl.html)
"""
from __future__ import absolute_import

import collections
import numpy as np
from obspy.core import Stream, Trace
from obspy.signal.util import nextpow2
import os

from . import finite_elem_mapping
from . import mesh
from . import rotations
from . import sem_derivatives
from . import spectral_basis
from . import lanczos
from instaseis.source import Source, ForceSource


MeshCollection_bwd = collections.namedtuple("MeshCollection_bwd", ["px", "pz"])
MeshCollection_fwd = collections.namedtuple("MeshCollection_fwd", ["m1", "m2",
                                                                   "m3", "m4"])

DEFAULT_MU = 32e9


class InstaSeis(object):
    """
    A class to extract Seismograms from a set of wavefields generated by
    AxiSEM. Taking advantage of reciprocity of the Green's function, two
    simulations with single force sources (vertical and horizontal) to build a
    complete Database of Green's function in global 1D models. The spatial
    discretization equals the SEM basis functions of AxiSEM, resulting in high
    order spatial accuracy and short access times.
    """
    def __init__(self, db_path, buffer_size_in_mb=100, read_on_demand=True,
                 reciprocal=True):
        """
        :param db_path: Path to the AxiSEM Database containing subdirectories
            PZ and/or PX each containing a order_output.nc4 file
        :type db_path: str
        :param buffer_size_in_mb: Strain is buffered to avoid unnecessary
            file access when sources are located in the same SEM element
        :type buffer_size_in_mb: int, optional
        :param read_on_demand: read several global fields on demand (faster
            initialization, default) or on initialization (faster in individual
            seismogram extraction, useful e.g. for finite sources)
        :type read_on_demand: bool, optional
        :param reciprocal: assume a reciprocal database (fixed receiver depth,
            sources anywhere) or a forward database (fixed source depth,
            receiver anywhere)
        :type reciprocal: bool, optional
        """
        self.db_path = db_path
        self.buffer_size_in_mb = buffer_size_in_mb
        self.read_on_demand = read_on_demand
        self._find_and_open_files(reciprocal=reciprocal)
        self.nfft = nextpow2(self.ndumps) * 2
        self.reciprocal = reciprocal
        self.planet_radius = self.parsed_mesh.planet_radius
        self.dump_type = self.parsed_mesh.dump_type

    def _find_and_open_files(self, reciprocal=True):
        if reciprocal:
            px = os.path.join(self.db_path, "PX")
            pz = os.path.join(self.db_path, "PZ")
            if not os.path.exists(px) and not os.path.exists(pz):
                raise ValueError(
                    "Expecting the 'PX' or 'PZ' subfolders to be present.")

            px_file = os.path.join(px, "Data", "ordered_output.nc4")
            pz_file = os.path.join(pz, "Data", "ordered_output.nc4")

            x_exists = os.path.exists(px_file)
            z_exists = os.path.exists(pz_file)

            # full_parse will force the kd-tree to be built
            if x_exists and z_exists:
                px_m = mesh.Mesh(
                    px_file, full_parse=True,
                    strain_buffer_size_in_mb=self.buffer_size_in_mb,
                    displ_buffer_size_in_mb=0,
                    read_on_demand=self.read_on_demand)
                pz_m = mesh.Mesh(
                    pz_file, full_parse=False,
                    strain_buffer_size_in_mb=self.buffer_size_in_mb,
                    displ_buffer_size_in_mb=0,
                    read_on_demand=self.read_on_demand)
                self.parsed_mesh = px_m
            elif x_exists:
                px_m = mesh.Mesh(
                    px_file, full_parse=True,
                    strain_buffer_size_in_mb=self.buffer_size_in_mb,
                    displ_buffer_size_in_mb=0,
                    read_on_demand=self.read_on_demand)
                pz_m = None
                self.parsed_mesh = px_m
            elif z_exists:
                px_m = None
                pz_m = mesh.Mesh(
                    pz_file, full_parse=True,
                    strain_buffer_size_in_mb=self.buffer_size_in_mb,
                    displ_buffer_size_in_mb=0,
                    read_on_demand=self.read_on_demand)
                self.parsed_mesh = pz_m
            else:
                raise ValueError("ordered_output.nc4 files must exist in the "
                                 "PZ/Data and/or PX/Data subfolders")

            self.meshes = MeshCollection_bwd(px_m, pz_m)
        else:
            m1 = os.path.join(self.db_path, "MZZ")
            m2 = os.path.join(self.db_path, "MXX_P_MYY")
            m3 = os.path.join(self.db_path, "MXZ_MYZ")
            m4 = os.path.join(self.db_path, "MXY_MXX_M_MYY")

            # important difference to reciprocal: forward only makes sens if
            # all subfolders are present
            if (not os.path.exists(m1) or not os.path.exists(m2) or not
               os.path.exists(m3) or not os.path.exists(m4)):
                raise ValueError(
                    "Expecting the four elemental moment tensor subfolders "
                    "to be present.")

            m1_file = os.path.join(m1, "Data", "ordered_output.nc4")
            m2_file = os.path.join(m2, "Data", "ordered_output.nc4")
            m3_file = os.path.join(m3, "Data", "ordered_output.nc4")
            m4_file = os.path.join(m4, "Data", "ordered_output.nc4")

            m1_exists = os.path.exists(m1_file)
            m2_exists = os.path.exists(m2_file)
            m3_exists = os.path.exists(m3_file)
            m4_exists = os.path.exists(m4_file)

            if m1_exists and m2_exists and m3_exists and m4_exists:
                m1_m = mesh.Mesh(
                    m1_file, full_parse=True, strain_buffer_size_in_mb=0,
                    displ_buffer_size_in_mb=self.buffer_size_in_mb,
                    read_on_demand=self.read_on_demand)
                m2_m = mesh.Mesh(
                    m2_file, full_parse=False, strain_buffer_size_in_mb=0,
                    displ_buffer_size_in_mb=self.buffer_size_in_mb,
                    read_on_demand=self.read_on_demand)
                m3_m = mesh.Mesh(
                    m3_file, full_parse=False, strain_buffer_size_in_mb=0,
                    displ_buffer_size_in_mb=self.buffer_size_in_mb,
                    read_on_demand=self.read_on_demand)
                m4_m = mesh.Mesh(
                    m4_file, full_parse=False, strain_buffer_size_in_mb=0,
                    displ_buffer_size_in_mb=self.buffer_size_in_mb,
                    read_on_demand=self.read_on_demand)
                self.parsed_mesh = m1_m
            else:
                raise ValueError("ordered_output.nc4 files must exist in the "
                                 "*/Data subfolders")

            self.meshes = MeshCollection_fwd(m1_m, m2_m, m3_m, m4_m)

    def get_seismograms(self, source, receiver, components=("Z", "N", "E"),
                        remove_source_shift=True, reconvolve_stf=False,
                        return_obspy_stream=True, dt=None, a_lanczos=5):
        """
        Extract seismograms for a moment tensor point source from the AxiSEM
        database.

        :param source: instaseis.Source or instaseis.ForceSource object
        :type source: :class:`instaseis.source.Source` or
            :class:`instaseis.source.ForceSource`
        :param receiver: instaseis.Receiver object
        :type receiver: :class:`instaseis.source.Receiver`
        :param components: a tuple containing any combination of the
            strings ``"Z"``, ``"N"`, ``"E"`, ``"R"``, and ``"T"``
        :param remove_source_shift: move the starttime to the peak of the
            sliprate from the source time function used to generate the
            database
        :param reconvolve_stf: deconvolve the source time function used in
            the AxiSEM run and convolve with the stf attached to the source.
            For this to be stable, the new stf needs to bandlimited.
        :param return_obspy_stream: return format is either an obspy.Stream
            object or a plain array containing the data
        :param dt: desired sampling of the seismograms. resampling is done
            using a lanczos kernel
        :param a_lanczos: width of the kernel used in resampling
        """
        if self.reciprocal:
            rotmesh_s, rotmesh_phi, rotmesh_z = rotations.rotate_frame_rd(
                source.x(planet_radius=self.planet_radius),
                source.y(planet_radius=self.planet_radius),
                source.z(planet_radius=self.planet_radius),
                receiver.longitude, receiver.colatitude)
        else:
            rotmesh_s, rotmesh_phi, rotmesh_z = rotations.rotate_frame_rd(
                receiver.x(planet_radius=self.planet_radius),
                receiver.y(planet_radius=self.planet_radius),
                receiver.z(planet_radius=self.planet_radius),
                source.longitude, source.colatitude)

        k_map = {"displ_only": 6,
                 "strain_only": 1,
                 "fullfields": 1}

        nextpoints = self.parsed_mesh.kdtree.query([rotmesh_s, rotmesh_z],
                                                   k=k_map[self.dump_type])

        # Find the element containing the point of interest.
        mesh = self.parsed_mesh.f.groups["Mesh"]
        if self.dump_type == 'displ_only':
            for idx in nextpoints[1]:
                corner_points = np.empty((4, 2), dtype="float64")

                if not self.read_on_demand:
                    corner_point_ids = self.parsed_mesh.fem_mesh[idx][:4]
                    eltype = self.parsed_mesh.eltypes[idx]
                    corner_points[:, 0] = \
                        self.parsed_mesh.mesh_S[corner_point_ids]
                    corner_points[:, 1] = \
                        self.parsed_mesh.mesh_Z[corner_point_ids]
                else:
                    corner_point_ids = mesh.variables["fem_mesh"][idx][:4]
                    eltype = mesh.variables["eltype"][idx]
                    corner_points[:, 0] = \
                        mesh.variables["mesh_S"][corner_point_ids]
                    corner_points[:, 1] = \
                        mesh.variables["mesh_Z"][corner_point_ids]

                isin, xi, eta = finite_elem_mapping.inside_element(
                    rotmesh_s, rotmesh_z, corner_points, eltype,
                    tolerance=1E-3)
                if isin:
                    id_elem = idx
                    break
            else:
                raise ValueError("Element not found")

            if not self.read_on_demand:
                gll_point_ids = self.parsed_mesh.sem_mesh[id_elem]
                axis = bool(self.parsed_mesh.axis[id_elem])
            else:
                gll_point_ids = mesh.variables["sem_mesh"][id_elem]
                axis = bool(mesh.variables["axis"][id_elem])

            if axis:
                col_points_xi = self.parsed_mesh.glj_points
                col_points_eta = self.parsed_mesh.gll_points
            else:
                col_points_xi = self.parsed_mesh.gll_points
                col_points_eta = self.parsed_mesh.gll_points
        else:
            id_elem = nextpoints[1]

        data = {}

        if self.reciprocal:

            fac_1_map = {"N": np.cos,
                         "E": np.sin}
            fac_2_map = {"N": lambda x: - np.sin(x),
                         "E": np.cos}

            if isinstance(source, Source):
                if self.dump_type == 'displ_only':
                    if axis:
                        G = self.parsed_mesh.G2
                        GT = self.parsed_mesh.G1T
                    else:
                        G = self.parsed_mesh.G2
                        GT = self.parsed_mesh.G2T

                strain_x = None
                strain_z = None

                # Minor optimization: Only read if actually requested.
                if "Z" in components:
                    if self.dump_type == 'displ_only':
                        strain_z = self.__get_strain_interp(
                            self.meshes.pz, id_elem, gll_point_ids, G, GT,
                            col_points_xi, col_points_eta, corner_points,
                            eltype, axis, xi, eta)
                    elif (self.dump_type == 'fullfields'
                            or self.dump_type == 'strain_only'):
                        strain_z = self.__get_strain(self.meshes.pz, id_elem)

                if any(comp in components for comp in ['N', 'E', 'R', 'T']):
                    if self.dump_type == 'displ_only':
                        strain_x = self.__get_strain_interp(
                            self.meshes.px, id_elem, gll_point_ids, G, GT,
                            col_points_xi, col_points_eta, corner_points,
                            eltype, axis, xi, eta)
                    elif (self.dump_type == 'fullfields'
                            or self.dump_type == 'strain_only'):
                        strain_x = self.__get_strain(self.meshes.px, id_elem)

                mij = rotations\
                    .rotate_symm_tensor_voigt_xyz_src_to_xyz_earth(
                        source.tensor_voigt, np.deg2rad(source.longitude),
                        np.deg2rad(source.colatitude))
                mij = rotations\
                    .rotate_symm_tensor_voigt_xyz_earth_to_xyz_src(
                        mij, np.deg2rad(receiver.longitude),
                        np.deg2rad(receiver.colatitude))
                mij = rotations.rotate_symm_tensor_voigt_xyz_to_src(
                    mij, rotmesh_phi)
                mij /= self.parsed_mesh.amplitude

                if "Z" in components:
                    final = np.zeros(strain_z.shape[0], dtype="float64")
                    for i in xrange(3):
                        final += mij[i] * strain_z[:, i]
                    final += 2.0 * mij[4] * strain_z[:, 4]
                    data["Z"] = final

                if "R" in components:
                    final = np.zeros(strain_x.shape[0], dtype="float64")
                    final -= strain_x[:, 0] * mij[0] * 1.0
                    final -= strain_x[:, 1] * mij[1] * 1.0
                    final -= strain_x[:, 2] * mij[2] * 1.0
                    final -= strain_x[:, 4] * mij[4] * 2.0
                    data["R"] = final

                if "T" in components:
                    final = np.zeros(strain_x.shape[0], dtype="float64")
                    final += strain_x[:, 3] * mij[3] * 2.0
                    final += strain_x[:, 5] * mij[5] * 2.0
                    data["T"] = final

                for comp in ["E", "N"]:
                    if comp not in components:
                        continue

                    fac_1 = fac_1_map[comp](rotmesh_phi)
                    fac_2 = fac_2_map[comp](rotmesh_phi)

                    final = np.zeros(strain_x.shape[0], dtype="float64")
                    final += strain_x[:, 0] * mij[0] * 1.0 * fac_1
                    final += strain_x[:, 1] * mij[1] * 1.0 * fac_1
                    final += strain_x[:, 2] * mij[2] * 1.0 * fac_1
                    final += strain_x[:, 3] * mij[3] * 2.0 * fac_2
                    final += strain_x[:, 4] * mij[4] * 2.0 * fac_1
                    final += strain_x[:, 5] * mij[5] * 2.0 * fac_2
                    if comp == "N":
                        final *= -1.0
                    data[comp] = final

            elif isinstance(source, ForceSource):
                if self.dump_type != 'displ_only':
                    raise ValueError("Force sources only in displ_only mode")

                if "Z" in components:
                    displ_z = self.__get_displacement(self.meshes.pz, id_elem,
                                                      gll_point_ids,
                                                      col_points_xi,
                                                      col_points_eta, xi, eta)

                if any(comp in components for comp in ['N', 'E', 'R', 'T']):
                    displ_x = self.__get_displacement(self.meshes.px, id_elem,
                                                      gll_point_ids,
                                                      col_points_xi,
                                                      col_points_eta, xi, eta)

                force = rotations.rotate_vector_xyz_src_to_xyz_earth(
                    source.force_tpr, np.deg2rad(source.longitude),
                    np.deg2rad(source.colatitude))
                force = rotations.rotate_vector_xyz_earth_to_xyz_src(
                    force, np.deg2rad(receiver.longitude),
                    np.deg2rad(receiver.colatitude))
                force = rotations.rotate_vector_xyz_to_src(
                    force, rotmesh_phi)
                force /= self.parsed_mesh.amplitude

                if "Z" in components:
                    final = np.zeros(displ_z.shape[0], dtype="float64")
                    final += displ_z[:, 0] * force[0]
                    final += displ_z[:, 2] * force[2]
                    data["Z"] = final

                if "R" in components:
                    final = np.zeros(displ_x.shape[0], dtype="float64")
                    final += displ_x[:, 0] * force[0]
                    final += displ_x[:, 2] * force[2]
                    data["R"] = final

                if "T" in components:
                    final = np.zeros(displ_x.shape[0], dtype="float64")
                    final += displ_x[:, 1] * force[1]
                    data["T"] = final

                for comp in ["E", "N"]:
                    if comp not in components:
                        continue

                    fac_1 = fac_1_map[comp](rotmesh_phi)
                    fac_2 = fac_2_map[comp](rotmesh_phi)

                    final = np.zeros(displ_x.shape[0], dtype="float64")
                    final += displ_x[:, 0] * force[0] * fac_1
                    final += displ_x[:, 1] * force[1] * fac_2
                    final += displ_x[:, 2] * force[2] * fac_1
                    if comp == "N":
                        final *= -1.0
                    data[comp] = final

            else:
                raise NotImplementedError

        else:
            if not isinstance(source, Source):
                raise NotImplementedError
            if self.dump_type != 'displ_only':
                raise NotImplementedError

            displ_1 = self.__get_displacement(self.meshes.m1, id_elem,
                                              gll_point_ids, col_points_xi,
                                              col_points_eta, xi, eta)
            displ_2 = self.__get_displacement(self.meshes.m2, id_elem,
                                              gll_point_ids, col_points_xi,
                                              col_points_eta, xi, eta)
            displ_3 = self.__get_displacement(self.meshes.m3, id_elem,
                                              gll_point_ids, col_points_xi,
                                              col_points_eta, xi, eta)
            displ_4 = self.__get_displacement(self.meshes.m4, id_elem,
                                              gll_point_ids, col_points_xi,
                                              col_points_eta, xi, eta)

            mij = source.tensor / self.parsed_mesh.amplitude
            # mij is [m_rr, m_tt, m_pp, m_rt, m_rp, m_tp]
            # final is in s, phi, z coordinates
            final = np.zeros((displ_1.shape[0], 3), dtype="float64")

            final[:, 0] += displ_1[:, 0] * mij[0]
            final[:, 2] += displ_1[:, 2] * mij[0]

            final[:, 0] += displ_2[:, 0] * (mij[1] + mij[2])
            final[:, 2] += displ_2[:, 2] * (mij[1] + mij[2])

            fac_1 = mij[3] * np.cos(rotmesh_phi) \
                + mij[4] * np.sin(rotmesh_phi)
            fac_2 = -mij[3] * np.sin(rotmesh_phi) \
                + mij[4] * np.cos(rotmesh_phi)

            final[:, 0] += displ_3[:, 0] * fac_1
            final[:, 1] += displ_3[:, 1] * fac_2
            final[:, 2] += displ_3[:, 2] * fac_1

            fac_1 = (mij[1] - mij[2]) * np.cos(2 * rotmesh_phi) \
                + 2 * mij[5] * np.sin(2 * rotmesh_phi)
            fac_2 = -(mij[1] - mij[2]) * np.sin(2 * rotmesh_phi) \
                + 2 * mij[5] * np.cos(2 * rotmesh_phi)

            final[:, 0] += displ_4[:, 0] * fac_1
            final[:, 1] += displ_4[:, 1] * fac_2
            final[:, 2] += displ_4[:, 2] * fac_1

            rotmesh_colat = np.arctan2(rotmesh_s, rotmesh_z)

            if "T" in components:
                # need the - for consistency with reciprocal mode,
                # need external verification still
                data["T"] = -final[:, 1]

            if "R" in components:
                data["R"] = final[:, 0] * np.cos(rotmesh_colat) \
                    - final[:, 2] * np.sin(rotmesh_colat)

            if "N" in components or "E" in components or "Z" in components:
                # transpose needed because rotations assume different slicing
                # (ugly)
                final = rotations.rotate_vector_src_to_NEZ(
                    final.T, rotmesh_phi,
                    source.longitude_rad, source.colatitude_rad,
                    receiver.longitude_rad, receiver.colatitude_rad).T

                if "N" in components:
                    data["N"] = final[:, 0]
                if "E" in components:
                    data["E"] = final[:, 1]
                if "Z" in components:
                    data["Z"] = final[:, 2]

        for comp in components:
            if remove_source_shift and not reconvolve_stf:
                data[comp] = data[comp][self.parsed_mesh.source_shift_samp:]
            elif reconvolve_stf:
                if source.dt is None or source.sliprate is None:
                    raise RuntimeError("source has no source time function")

                stf_deconv_f = np.fft.rfft(
                    self.sliprate, n=self.nfft)

                if abs((source.dt - self.dt) / self.dt) > 1e-7:
                    raise ValueError("dt of the source not compatible")

                stf_conv_f = np.fft.rfft(source.sliprate,
                                         n=self.nfft)

                if source.time_shift is not None:
                    stf_conv_f *= \
                        np.exp(- 1j * np.fft.rfftfreq(self.nfft)
                               * 2. * np.pi * source.time_shift / self.dt)

                # TODO: double check wether a taper is needed at the end of the
                #       trace
                dataf = np.fft.rfft(data[comp], n=self.nfft)

                data[comp] = np.fft.irfft(
                    dataf * stf_conv_f / stf_deconv_f)[:self.ndumps]

            if dt is not None:
                data[comp] = lanczos.lanczos_resamp(
                    data[comp], self.parsed_mesh.dt, dt, a_lanczos)

        if return_obspy_stream:
            # Convert to an ObsPy Stream object.
            st = Stream()
            if dt is None:
                dt = self.parsed_mesh.dt
            band_code = self._get_band_code(dt)
            for comp in components:
                tr = Trace(data=data[comp],
                           header={"delta": dt,
                                   "station": receiver.station,
                                   "network": receiver.network,
                                   "channel": "%sX%s" % (band_code, comp)})
                st += tr
            return st
        else:
            npol = self.parsed_mesh.npol
            if not self.read_on_demand:
                mu = self.parsed_mesh.mesh_mu[gll_point_ids[npol/2, npol/2]]
            else:
                mu = mesh.variables["mesh_mu"][gll_point_ids[npol/2, npol/2]]
            return data, mu

    def get_seismograms_finite_source(self, sources, receiver,
                                      components=("Z", "N", "E"), dt=None,
                                      a_lanczos=5):
        """
        Extract seismograms for a finite source from the AxiSEM database
        provided as a list of point sources attached with source time functions
        and time shifts.

        :param sources: A collection of point sources.
        :type sources: list of :class:`instaseis.source.Source` objects
        :param receiver: The receiver location.
        :type receiver: :class:`instaseis.source.Receiver`
        :param components: a tuple containing any combination of the strings
            ``"Z"``, ``"N"``, and ``"E"``
        :param dt: desired sampling of the seismograms.resampling is done
            using a lanczos kernel
        :param a_lanczos: width of the kernel used in resampling
        """
        if not self.reciprocal:
            raise NotImplementedError

        data_summed = {}
        for source in sources:
            data, mu = self.get_seismograms(
                source, receiver, components, reconvolve_stf=True,
                return_obspy_stream=False)
            for comp in components:
                if comp in data_summed:
                    data_summed[comp] += data[comp] * mu / DEFAULT_MU
                else:
                    data_summed[comp] = data[comp] * mu / DEFAULT_MU

        if dt is not None:
            for comp in components:
                data_summed[comp] = lanczos.lanczos_resamp(
                    data_summed[comp], self.parsed_mesh.dt, dt, a_lanczos)

        # Convert to an ObsPy Stream object.
        st = Stream()
        if dt is None:
            dt = self.parsed_mesh.dt
        band_code = self._get_band_code(dt)
        for comp in components:
            tr = Trace(data=data_summed[comp],
                       header={"delta": dt,
                               "station": receiver.station,
                               "network": receiver.network,
                               "channel": "%sX%s" % (band_code, comp)})
            st += tr
        return st

    def _get_band_code(self, dt):
        """
        Figure out the channel band code. Done as in SPECFEM.
        """
        sr = 1.0 / dt
        if sr <= 0.001:
            band_code = "F"
        elif sr <= 0.004:
            band_code = "C"
        elif sr <= 0.0125:
            band_code = "H"
        elif sr <= 0.1:
            band_code = "B"
        elif sr <= 1:
            band_code = "M"
        else:
            band_code = "L"
        return band_code

    def __get_strain_interp(self, mesh, id_elem, gll_point_ids, G, GT,
                            col_points_xi, col_points_eta, corner_points,
                            eltype, axis, xi, eta):
        if id_elem not in mesh.strain_buffer:
            # Single precision in the NetCDF files but the later interpolation
            # routines require double precision. Assignment to this array will
            # force a cast.
            utemp = np.zeros((mesh.ndumps, mesh.npol + 1, mesh.npol + 1, 3),
                             dtype=np.float64, order="F")

            mesh_dict = mesh.f.groups["Snapshots"].variables

            # Load displacement from all GLL points.
            for i, var in enumerate(["disp_s", "disp_p", "disp_z"]):
                if var not in mesh_dict:
                    continue
                temp = mesh_dict[var][:, gll_point_ids.flatten()]
                for ipol in xrange(mesh.npol + 1):
                    for jpol in xrange(mesh.npol + 1):
                        utemp[:, jpol, ipol, i] = temp[:, ipol * 5 + jpol]

            strain_fct_map = {
                "monopole": sem_derivatives.strain_monopole_td,
                "dipole": sem_derivatives.strain_dipole_td,
                "quadpole": sem_derivatives.strain_quadpole_td}

            strain = strain_fct_map[mesh.excitation_type](
                utemp, G, GT, col_points_xi, col_points_eta, mesh.npol,
                mesh.ndumps, corner_points, eltype, axis)

            mesh.strain_buffer.add(id_elem, strain)
        else:
            strain = mesh.strain_buffer.get(id_elem)

        final_strain = np.empty((strain.shape[0], 6), order="F")

        for i in xrange(6):
            final_strain[:, i] = spectral_basis.lagrange_interpol_2D_td(
                col_points_xi, col_points_eta, strain[:, :, :, i], xi, eta)

        if not mesh.excitation_type == "monopole":
            final_strain[:, 3] *= -1.0
            final_strain[:, 5] *= -1.0

        return final_strain

    def __get_strain(self, mesh, id_elem):
        if id_elem not in mesh.strain_buffer:
            strain_temp = np.zeros((self.ndumps, 6), order="F")

            mesh_dict = mesh.f.groups["Snapshots"].variables

            for i, var in enumerate([
                    'strain_dsus', 'strain_dsuz', 'strain_dpup',
                    'strain_dsup', 'strain_dzup', 'straintrace']):
                if var not in mesh_dict:
                    continue
                strain_temp[:, i] = mesh_dict[var][:, id_elem]

            # transform strain to voigt mapping
            # dsus, dpup, dzuz, dzup, dsuz, dsup
            final_strain = np.empty((self.ndumps, 6), order="F")
            final_strain[:, 0] = strain_temp[:, 0]
            final_strain[:, 1] = strain_temp[:, 2]
            final_strain[:, 2] = (strain_temp[:, 5] - strain_temp[:, 0]
                                  - strain_temp[:, 2])
            final_strain[:, 3] = -strain_temp[:, 4]
            final_strain[:, 4] = strain_temp[:, 1]
            final_strain[:, 5] = -strain_temp[:, 3]
            mesh.strain_buffer.add(id_elem, final_strain)
        else:
            final_strain = mesh.strain_buffer.get(id_elem)

        return final_strain

    def __get_displacement(self, mesh, id_elem, gll_point_ids, col_points_xi,
                           col_points_eta, xi, eta):
        if id_elem not in mesh.displ_buffer:
            utemp = np.zeros((mesh.ndumps, mesh.npol + 1, mesh.npol + 1, 3),
                             dtype=np.float64, order="F")

            mesh_dict = mesh.f.groups["Snapshots"].variables

            # Load displacement from all GLL points.
            for i, var in enumerate(["disp_s", "disp_p", "disp_z"]):
                if var not in mesh_dict:
                    continue
                temp = mesh_dict[var][:, gll_point_ids.flatten()]
                for ipol in xrange(mesh.npol + 1):
                    for jpol in xrange(mesh.npol + 1):
                        utemp[:, jpol, ipol, i] = temp[:, ipol * 5 + jpol]

            mesh.displ_buffer.add(id_elem, utemp)
        else:
            utemp = mesh.displ_buffer.get(id_elem)

        final_displacement = np.empty((utemp.shape[0], 3), order="F")

        for i in xrange(3):
            final_displacement[:, i] = spectral_basis.lagrange_interpol_2D_td(
                col_points_xi, col_points_eta, utemp[:, :, :, i], xi, eta)

        return final_displacement

    @property
    def dt(self):
        return self.parsed_mesh.dt

    @property
    def ndumps(self):
        return self.parsed_mesh.ndumps

    @property
    def background_model(self):
        return self.parsed_mesh.background_model

    @property
    def attenuation(self):
        return self.parsed_mesh.attenuation

    @property
    def sliprate(self):
        return self.parsed_mesh.stf_d_norm

    @property
    def slip(self):
        return self.parsed_mesh.stf

    def __str__(self):

        if self.reciprocal:
            if self.meshes.pz is not None and self.meshes.px is not None:
                components = 'vertical and horizontal'
            elif self.meshes.pz is None and self.meshes.px is not None:
                components = 'horizontal only'
            elif self.meshes.pz is not None and self.meshes.px is None:
                components = 'vertical only'

            return_str = "AxiSEM reciprocal Green's function Database\n"
            return_str += "generated with these parameters:\n"
            return_str += 'components            : %s\n' % (components,)
        else:
            return_str = "AxiSEM forward Green's function Database\n"
            return_str += "generated with these parameters:\n"
            return_str += 'source depth          : %s\n' % \
                (self.parsed_mesh.source_depth,)

        return_str += 'velocity model        : %s\n' % (self.background_model,)
        return_str += 'attenuation           : %s\n' % (self.attenuation,)
        return_str += 'dominant period       : %6.3f s\n' % \
            (self.parsed_mesh.dominant_period,)
        return_str += 'dump type             : %s\n' % (self.dump_type,)
        return_str += 'time step             : %6.3f s\n' % (self.dt,)
        return_str += 'sampling rate         : %6.3f Hz\n' % (1./self.dt,)
        return_str += 'number of samples     : %6i\n' % (self.ndumps,)
        return_str += 'seismogram length     : %6.1f s\n' % \
            (self.dt * (self.ndumps - 1),)
        return_str += 'source time function  : %s\n' % \
            (self.parsed_mesh.stf,)
        return_str += 'source shift          : %6.3f s\n' % \
            (self.parsed_mesh.source_shift,)
        return_str += 'spatial order         : %6i\n' % \
            (self.parsed_mesh.npol,)

        return_str += 'min/max radius [km]   : %6.1f %6.1f\n' % \
            (self.parsed_mesh.kwf_rmin, self.parsed_mesh.kwf_rmax)
        return_str += 'min/max dist [degree] : %6.1f %6.1f\n' % \
            (self.parsed_mesh.kwf_colatmin, self.parsed_mesh.kwf_colatmax)

        return_str += 'time scheme           : %s\n' % \
            (self.parsed_mesh.time_scheme,)

        return return_str
