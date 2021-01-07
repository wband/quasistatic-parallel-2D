from mpi4py import MPI
import sys
import time
import numpy as np
import os

mu_build_path = "/home/fr/fr_fr/fr_wa1005/muspectre_stuff/builds/muspectre-20201214/build/"
sys.path.append(mu_build_path + '/language_bindings/python')
sys.path.append(mu_build_path + '/language_bindings/libmufft/python')
sys.path.append(mu_build_path + '/language_bindings/libmugrid/python')

#import NewtonCG
from constrainedCG import constrained_conjugate_gradients
import muSpectre as msp
import muSpectre.vtk_export as vt_ex
import muSpectre.gradient_integration as gi
import muFFT
import muGrid

class parallel_fracture():
    def __init__(self, Lx = 10, nx = 63, cfield=None):
        self.dim = 2
        self.lens = [Lx, Lx] #simulation cell lengths
        self.nb_grid_pts  = [nx, nx] #number of grid points in each spatial direction
        self.dx = np.array([Lx/nx, Lx/nx])
        self.comm = MPI.COMM_WORLD
        
        self.Young = 100.0
        self.Poisson = 0.0
        self.ksmall = 1e-4
        self.F_tot = np.array([[ 0.0,  0.0],
                  [ 0.0 , 0.00]])

        self.fftengine = muFFT.FFT(self.nb_grid_pts,fft='fftwmpi',communicator=self.comm)
        self.fourier_buffer   = self.fftengine.register_fourier_space_field("fourier-space", 1)
        self.x0               = np.zeros(self.fftengine.nb_subdomain_grid_pts)
        self.fourier_gradient = [msp.FourierDerivative(self.dim , i) for i in range(self.dim)]
        
        self.fc_glob = muGrid.GlobalFieldCollection(self.dim)
        self.fc_glob.initialise(self.nb_grid_pts,
                self.fftengine.nb_subdomain_grid_pts,
                self.fftengine.subdomain_locations)
        self.phi    = self.fc_glob.register_real_field("phi", 1)
        self.Cx     = self.fc_glob.register_real_field("Cx", 1)
        self.initialize_parallel_Cx()
        self.initialize_parallel_phi()
        self.strain = self.fc_glob.register_real_field("strain", (2,2))
        self.straineng = self.fc_glob.register_real_field("straineng", 1)
        self.phi_old = self.phi.array() + 0.0
        self.cell = msp.Cell(self.nb_grid_pts, self.lens ,msp.Formulation.small_strain,
            self.fourier_gradient,fft='fftwmpi',communicator=self.comm)
        self.material = self.initialize_material()
        self.cell.initialise()  #initialization of fft to make faster fft

        self.delta_energy_tol = 1e-6
        self.solver_tol = 1e-10
        self.maxiter_cg = 40000
        ### initialize strain calculation result and energy
        self.strain_result = self.strain_solver()
        self.total_energy = self.objective(self.phi.array())
        
### initialization of Cx
    def initialize_parallel_Cx(self):
        for ind, val in np.ndenumerate(self.Cx):
            coords = (np.array(ind) + np.array(self.fftengine.subdomain_locations))*self.dx
            self.Cx.array()[ind] = self.init_function_Cx(coords)*self.Young
        
    def initialize_parallel_phi(self):
        for ind, val in np.ndenumerate(self.Cx):
            coords = (np.array(ind) + np.array(self.fftengine.subdomain_locations))*self.dx
            self.phi.array()[ind] = self.init_function_phi(coords)

    def init_function_Cx(self, coords):
        val = 1.0
   #     distcoord = np.abs(coords - np.array(self.lens)/2)
   #     dist = np.sqrt(np.sum(distcoord**2)) - self.dx[0]
   #     if (dist < 1.0):
   #         val = 0.5-0.5*np.cos(np.pi/2*(dist))
   #     if (dist < 1.0):
   #         val = 0.0
   #     if (dist < 0):
   #         val = 0.0
        return val

    def init_function_phi(self, coords):
        val = 0.0
        distcoord = np.abs(coords - np.array(self.lens)/2)
        distcoord[0] = max(distcoord[0]-0.5,0)
        dist = np.sqrt(np.sum(distcoord**2)) - self.dx[0]
        if (dist < 2.0**0.5):
            val = (1-dist/2**0.5)**2
        return val


    def initialize_serial(self,fname):
        field = np.load(fname)
        field = np.ones(tuple(self.nb_grid_pts))
        self.Cx.array()[...] = field[self.fftengine.subdomain_locations[0]:
            self.fftengine.subdomain_locations[0]+self.fftengine.nb_subdomain_grid_pts[0],
            self.fftengine.subdomain_locations[1]:self.fftengine.subdomain_locations[1]
            +self.fftengine.nb_subdomain_grid_pts[1]]

    def initialize_material(self):
        material = msp.material.MaterialLinearElastic4_2d.make(self.cell, "material_small")
        for pixel, Cxval in np.ndenumerate(self.Cx.array()):
            pixel_id = pixel[0]+pixel[1]*self.fftengine.nb_subdomain_grid_pts[0]
            material.add_pixel(pixel_id, self.Cx.array()[tuple(pixel)]*
                 (1.0-self.phi.array()[tuple(pixel)])**2+self.ksmall, self.Poisson)
        return material

    def strain_solver(self):            
        ### set current material properties
        for pixel, Cxval in np.ndenumerate(self.Cx.array()):
            pixel_id = pixel[1]+pixel[0]*self.fftengine.nb_subdomain_grid_pts[1]
            #temp = self.material.get_youngs_modulus(pixel_id)
            self.material.set_youngs_modulus(pixel_id,
                   Cxval*(1.0-self.phi.array()[tuple(pixel)])**2+self.ksmall)
        
        ### run muSpectre computation
        verbose = msp.Verbosity.Silent
        solver = msp.solvers.KrylovSolverCG(self.cell, self.solver_tol, self.maxiter_cg, verbose)
        return msp.solvers.newton_cg(self.cell, self.F_tot, solver, self.solver_tol, self.solver_tol, verbose)
    
### key parts of test system
    def get_straineng(self):
        lamb_fact = self.Poisson/(1+self.Poisson)/(1-2*self.Poisson)
        mu_fact = 1/2/(1+self.Poisson)
        for pixel, Cxval in np.ndenumerate(self.Cx.array()):
            pixel_id = pixel[0]+pixel[1]*self.fftengine.nb_subdomain_grid_pts[0]
            strain = np.reshape(self.strain_result.grad[pixel_id*4:(pixel_id+1)*4],(2,2))
            self.strain.array()[:,:,0,pixel[0],pixel[1]] = strain
            trace = 0.0
            for k in range(0,self.dim):
                trace += strain[k,k]
            self.straineng.array()[tuple(pixel)] = 0.5*self.Cx.array()[tuple(pixel)]*(2.0*mu_fact*(strain**2).sum() 
                + lamb_fact*self.Cx.array()[tuple(pixel)]*trace**2)

    def laplacian(self,x):
        return_arr = np.zeros(self.fftengine.nb_subdomain_grid_pts)
        self.fftengine.fft(x, self.fourier_buffer)
        self.fftengine.ifft(-np.einsum('i...,i', self.fftengine.fftfreq**2, (2*np.pi/self.dx)**2)
            *self.fourier_buffer.array(), return_arr)
        return return_arr*self.fftengine.normalisation
        
    def grad2(self,x):
        self.fftengine.fft(x, self.fourier_buffer)
        return_arr = np.zeros(self.fftengine.nb_subdomain_grid_pts)
        tempfield  = np.zeros(self.fftengine.nb_subdomain_grid_pts)
        for d in range(0,self.dim):
            diffop = muFFT.FourierDerivative(self.dim, d)
            self.fftengine.ifft( diffop.fourier(self.fftengine.fftfreq)
                *self.dx[d]*self.fourier_buffer, tempfield)
            return_arr += tempfield**2
        return return_arr*self.fftengine.normalisation**2
        
    def energy_density(self,x):
        return (1.0-x)**2*self.straineng.array() + x + 0.5*self.grad2(x)

    def integrate(self,f):
        return self.comm.allreduce(np.sum(f)*np.prod(self.dx),MPI.SUM)
        
    def objective(self,x):
        return self.integrate(self.energy_density(x))

    def jacobian(self,x):
        return 2*(x-1.0)*self.straineng.array() + 1.0 - self.laplacian(x)

    def hessp(self,p):
        return 2.0*self.straineng.array()*p - self.laplacian(p) 

    def phi_solver(self):
        self.get_straineng()
        Jx = -self.jacobian(self.phi.array())
        update = constrained_conjugate_gradients(Jx, self.hessp, Jx,
                                 self.phi_old - self.phi.array(), self.comm)
        self.phi.array()[...] += update
           
                                 
  # currently broken, in the name of progress          
    def crappyIO(self,fname):  ## will replace this with a real parallel IO at some point
        np.save(fname+'rank{:02d}'.format(self.comm.rank),self.phi)
    
    def muOutput(self,fname,new=False):
        comm = muGrid.Communicator(self.comm)
        if (new):
            if(self.comm.rank == 0):
                if os.path.exists(fname):
                    os.remove(fname)
            file_io_object = muGrid.FileIONetCDF(
                fname, muGrid.FileIONetCDF.OpenMode.Write, comm)
        else:
            file_io_object = muGrid.FileIONetCDF(
                fname, muGrid.FileIONetCDF.OpenMode.Append, comm)
        file_io_object.register_field_collection(self.fc_glob)
        file_frame = file_io_object.append_frame()
        file_frame.write()
        file_io_object.close()

        

