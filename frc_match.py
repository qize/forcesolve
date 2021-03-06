# This file is part of ForceSolve, Copyright (C) 2008 David M. Rogers.
#
#   ForceSolve is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   ForceSolve is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with ForceSolve (i.e. frc_solve/COPYING).
#   If not, contact the author(s) immediately \at/ wantye \/ gmail.com or
#   http://forceSolve.sourceforge.net/. And see http://www.gnu.org/licenses/
#   for a copy of the GNU GPL.

# David M. Rogers
# Dr. Thomas Beck Lab
# University of Cincinnati
# 12/10/2007
# This work was supported by a DOE CSGF.

from numpy import *
import numpy.linalg as la
import numpy.random as rand
from os import path
from cg_topol.ucgrad import write_matrix, write_list
from cg_topol import write_topol, show_index
from scipy.optimize import fmin_cg #, newton_krylov

try:
    from quad_prog import quad_prog
except ImportError:
    print("Error Importing CVXOPT (required for parameter constraints).")
    quad_prog = None

# cg_topol uses integrated first, second and third derivatives for prior which,
# when multiplied by the corresponding values in alpha, serve to push the
# energy function toward a flat, linear, or quadratic shape -- respectively.

class frc_match:
	def __init__(self, topol, pdb, dt, kT, \
			E0=1.0e-8, calpha=1.0e+3, logalpha=None,
			do_nonlin = False):
		# No inputs defined yet...
		params = topol.params
		self.topol = topol
                self.pdb = pdb
                self.ind = topol.ind + [params]
		#assert topol.can_force_match()
		
		self.logalpha = logalpha
		self.do_nonlin = do_nonlin
		self.build_type_index()
		types = self.types
		
		self.dt = dt
		self.kT = kT
		
		#self.D = zeros((types,params))
		self.D2 = zeros((types,params,params))
		self.DF = zeros((types,params))
		self.F2 = zeros((types))
		self.S = 0
                self.rhs0 = zeros(params)
                self.iC0  = zeros((params, params))

		self.nonlin = {}
		# The following are only populated if nonlin != {}
		self.F = []     # all forces to match, [(S,N,3)]
		self.seeds = {"_lin":[]} # sufficient statistics, from nonlin(x)
				# {String : [(S,?)]}
		
		self.E0 = E0
		self.calpha = 100.0 # calpha
		#for i in range(len(self.topol.prior)):
		#    if self.prior_rank[i] != len(self.prior[i]):
	#		val, A = la.eigh(self.prior[i])
	#		for j in range(len(self.prior[i])-self.prior_rank[i]):
	#			val[j] = self.E0*self.E0*0.01
	#		self.prior[i] = dot(A*val[newaxis,:], transpose(A))
	#		#A = A[:,-self.prior_rank[i]:]
	#		#val = val[-self.prior_rank[i]:]
	#		#self.prior[i] = dot(A*val[newaxis,:], transpose(A))
		self.alpha = ones(self.topol.hyp_params)
		
		# Normalize.
		self.orthonormalize_constraints(array(self.topol.constraints))
		#self.constraints = array(self.topol.constraints)
                self.ineqs = array(self.topol.ineqs)
		
                self.dtheta = zeros(params)
		#self.theta0 = self.theta_from_topol() # Parameters.
		self.theta0 = zeros(params)
		# theta = theta0 + dtheta (improves numerical precision)
		self.z = ones(types)
		
		# Sampling accumulators.
		self.sum_v = []
		self.sum_alpha = []
		self.sum_dtheta = []
		self.sum_dmu2 = []
		self.sum_df2 = []
                self.sum_resid2 = []
		self.df2 = zeros(len(topol.ind))
		self.samples = 0
	
	# Nonlinear forces must be added before adding x,F data.
	#   c : P
	#   seed : (S,N,3), args -> (S,)+R
	#   F : P, (S,)+R,  args -> (S,N,3)
	#   J : P, (S,)+R,  args -> (S,N,3,P)
	#   args : ()
	def add_nonlin(self, name, c, seed, F, J, args=()):
	    assert self.S == 0, "Error! can't add_nonlin after inputting data."
	    if name == "_lin":
		raise ValueError, "_lin is a reserved name."
	    if self.nonlin.has_key(name):
		print "Strong Warning: replacing nonlin '%s'??!!?"%name
	    self.nonlin[name] = [c, seed, F, J, args]
	    self.seeds[name] = []

	# Build an index to atoms by designated atom type.
	def build_type_index(self):
                tmass = dict([(aname[2],m) for aname,m in
                                zip(self.pdb.names,
                                    self.pdb.mass)])
		self.type_names = tmass.keys()
		self.types = len(self.type_names)
		#print self.type_names
		
                # Dictionary for quick reference.
		type_num = dict([(t,i) for i,t in enumerate(self.type_names)])
		mass = array([tmass[n] for n in self.type_names])
		
		nt = [0]*self.types
		
		self.type_index = []
		for aname in self.pdb.names:
			tn = type_num[aname[2]]
			nt[tn] += 1
			self.type_index.append(tn)
		self.mass = self.pdb.mass
		self.nt = array(nt)
                #print self.nt, mass, self.type_index

	# Estimate the actual dimensionality of iC.
        # if fix == True, constrain all 'free' directions to zero.
	def dimensionality(self, sigma_tol=1.0e-5, fix=True):
		tol = sigma_tol*sigma_tol
		itol = 1.0/tol
		iC = self.calc_iC()
		w, A = la.eigh(iC)
		free_dim = len([l for l in w if l <= tol])
		fixed_dim = len([l for l in w if l >= itol])
		print "Free directions = %d, Fixed directions = %d"%(\
					free_dim, fixed_dim)
		if free_dim > 0:
                    C = []
		    for i in range(len(w)):
			if w[i] > tol:
				continue
			print "\tValue %e == %g"%(w[i], \
                                  dot(dot(iC,A[:,i]), A[:,i]))
			w[i] = 10.0*itol
                        C.append(A[:,i])
			#print "\tVector:"
			#print A[:,i]
                    if fix:
                        self.constraints, self.span = orthonormalize(
                                concatenate((self.constraints, C)) )
		return w, A

	# Append a set of data points to the present frc_match object.
	def append(self, x,f):
		chunk = 100
		
		if self.samples > 0:
			raise ValueError, "Error! Cannot add more data "\
				"points to mature frc_match object."
		if x.shape != f.shape:
			print x.shape, f.shape
			raise ValueError, "Error! x and f trajectory shapes "\
				"differ!"
		if x.shape[-2] != self.pdb.atoms:
			raise ValueError, "Error! Number of atoms in "\
				"trajectory does not match topology!"
                if x.shape[-1] != 3:
			raise ValueError, "Error! last dim should be crd xyz!"
		
		# Multiply by ugly constants here.
		Xfac = self.dt*sqrt(self.kT/self.mass) # Non-dimensionalize.
		f *= (self.dt/sqrt(self.mass*self.kT))[newaxis,:,newaxis]

		print "Appending %d samples..."%(len(x))
		self.S += len(x)
		# Operate on "chunk" structures at once.
		for i in range(0,len(x)-chunk+1,chunk):
		    fhat = 0.0
		    for k,v in self.nonlin.iteritems():
			self.seeds[k].append(v[1](x[i:i+chunk], *v[4]))
			fhat += v[2](v[0], self.seeds[k][-1], *v[4])
		    fhat *= (self.dt/sqrt(self.mass*self.kT))[newaxis,:,newaxis]

		    D = -1.0*self.topol.design(x[i:i+chunk],1)[1] \
			* Xfac[newaxis,:,newaxis,newaxis] # Factor cancels 1/dx
		    if len(self.nonlin) > 0:
			self.F.append(f[i:i+chunk])
			self.seeds["_lin"].append(D)
		    if any(abs(self.theta0) > 0.0):
			fhat += dot(D, self.theta0) # subtract fixed contrib.
		    #self.D += self.type_sum(sum(sum(D,-2),0))
		    self.type_sum_D2(D)
		    self.type_sum_DF(D, f[i:i+chunk]-fhat)
		    self.F2 += self.type_sum(sum(sum(\
					(f[i:i+chunk]-fhat)**2,-1),0))
		
		i = len(x)%chunk
		if i != 0:
		    i = len(x)-i
		    fhat = 0.0
		    for k,v in self.nonlin.iteritems():
			self.seeds[k].append(v[1](x[i:], *v[4]))
			fhat += v[2](v[0], self.seeds[k][-1], *v[4])
		    fhat *= (self.dt/sqrt(self.mass*self.kT))[newaxis,:,newaxis]

		    D = -1.0*self.topol.design(x[i:],1)[1] \
			* Xfac[newaxis,:,newaxis,newaxis] # Factor cancels 1/dx
		    if len(self.nonlin) > 0:
			self.F.append(f[i:])
			self.seeds["_lin"].append(D)
		    if any(abs(self.theta0) > 0.0):
			fhat += dot(D, self.theta0) # subtract fixed contrib.
		    #self.D += self.type_sum(sum(sum(D,-2),0))
		    self.type_sum_D2(D)
		    self.type_sum_DF(D, f[i:]-fhat)
		    self.F2 += self.type_sum(sum(sum(\
					(f[i:]-fhat)**2,-1),0))
		
		# 1 structure at a time.
		#for xi, fi in zip(x,f):# Design matrices are for energy deriv.s
		#	D = -1.0*self.topol.design(xi,1)[1] # -ize -> F.
		#	self.D += self.type_sum(sum(D, 1))
		#	self.D2 += self.type_sum(sum(\
		#		D[:,:,:,newaxis]*D[:,:,newaxis,:],1))
		#	self.DF += self.type_sum(sum(D*fi[:,:,newaxis],1))
		#	self.F2 += self.type_sum(sum(fi*fi,1))
	
	# Improve precision by shifting theta
	# (requires F and D were kept around).
	def set_theta0(self, t0):
	    if len(self.nonlin) == 0:
		return # Just smile and nod.

	    # shift theta values
	    for dt in self.sum_dtheta:
		dt += self.theta0 - t0
	    self.dtheta += self.theta0 - t0
	    self.theta0 = t0
            self.recompute_lin()

	# Re-compute the linear parts of the fit.
	# This must be called if the nonlinear coeffs. change.
	def recompute_lin(self):
		if len(self.nonlin) == 0: # what-um?
		    return

		self.DF[:] = 0.0
		self.F2[:] = 0.0

		# Operate on "chunk" structures at once.
		for i in range(len(self.F)):
		    fhat = 0.0
		    for k,v in self.nonlin.iteritems():
			fhat += v[2](v[0], self.seeds[k][i], *v[4])
		    fhat *= (self.dt/sqrt(self.mass*self.kT))[newaxis,:,newaxis]

		    D = self.seeds["_lin"][i]
		    fhat += dot(D, self.theta0) # subtract fixed contrib.
		    self.type_sum_DF(D, self.F[i]-fhat)
		    self.F2 += self.type_sum(sum(sum((self.F[i]-fhat)**2,-1),0))
	
	# Pre-compute the nonlinear residual for nonlin "name"
	# i.e.  resid = (forces from all terms except "name") - F
	# and returns closures for computing err and d(err)/dc
	def resid_nonlin(self, name):
		assert self.nonlin.has_key(name), \
			"Nonlin. type %s not present."%name
		df = []
		Fscale = (self.dt/sqrt(self.mass*self.kT))[newaxis,:,newaxis]

		# Operate on "chunk" structures at once.
		for i in range(len(self.F)):
		    fhat = 0.0
		    for k,v in self.nonlin.iteritems():
			if k == name:
			    continue
			fhat += v[2](v[0], self.seeds[k][i], *v[4])
		    if len(self.nonlin) > 1:
		        fhat *= Fscale

		    fhat += dot(self.seeds["_lin"][i], self.theta0+self.dtheta)
		    df.append(fhat - self.F[i])

		v = self.nonlin[name]
		def RR(x):
		    err = 0.0
		    for i in range(len(self.F)):
			R = df[i] + v[2](x, self.seeds[name][i], *v[4]) \
		                       * Fscale
			for a,t in enumerate(self.type_index):
			    err += self.z[t]*tensordot(R[:,a], R[:,a], 2)
		    return 0.5*err

		def RJ_JJ(x):
		    RJ = 0.0
		    JJ = 0.0
		    n = len(x)
		    for i in range(len(self.F)):
			R = df[i] + v[2](x, self.seeds[name][i], *v[4]) \
					* Fscale
			J = v[3](x, self.seeds[name][i], *v[4]) \
					* Fscale[...,newaxis]
			m = len(R)*3
			for a,t in enumerate(self.type_index):
			    RJ += self.z[t]*tensordot(R[:,a], J[:,a], 2)
			    JJ += self.z[t]*dot(
				    transpose(J[:,a].reshape((m,n))),
					      J[:,a].reshape((m,n)) )
		    return RJ, JJ

		return RR, RJ_JJ

	# Given an (atoms by ?) array, reduce to a (types by ?) array
	# by summation.
	def type_sum(self, x):
		xt = zeros((self.types,)+x.shape[1:])
		for i,t in enumerate(self.type_index):
			xt[t] += x[i]
		return xt
	# Special type_sum for accumulating D2 matrices.
	# the numerics are sensitive to how this is computed...
	def type_sum_D2(self, DS):
		#for D in DS:
		#  for i,t in enumerate(self.type_index):
		#    for j in range(3):
		#	self.D2[t] += D[i,j,:,newaxis]*D[i,j,newaxis,:]
		for i,t in enumerate(self.type_index):
		    self.D2[t] += tensordot(DS[:,i], DS[:,i], \
					    axes=[(0,1),(0,1)])
	# Special type_sum for accumulating DF matrices.
	def type_sum_DF(self, DS, FS):
		#for D, F in zip(DS, FS):
		#  for i,t in enumerate(self.type_index):
		#    for j in range(3):
		#	self.DF[t] += D[i,j]*F[i,j,newaxis]
		for i,t in enumerate(self.type_index):
		    self.DF[t] += tensordot(DS[:,i], FS[:,i], \
					    axes=[(0,1),(0,1)])

	# Make constraints orthonormal.
        # and complete the complementary (perpendicular) subspace.
	def orthonormalize_constraints(self, constraints):
            if constraints is None:
                self.constraints = zeros((0,self.topol.params))
                self.span = identity(self.topol.params)
            else:
                self.constraints, self.span = orthonormalize(constraints)

	# Find maximum likelihood estimate.
	def maximize(self, tol=1.0e-5, maxiter=100):
		if self.S < 1:
			raise ProgramError, "Error! no data has been collected!"
		
		print "Maximizing posterior PDF..."
		az, ibz, aa, iba = self.calc_za_ab()
		# const. - log(P)
		llp = dot(ibz,self.z) - dot(az-1,log(self.z)) \
			+ dot(iba,self.alpha) - dot(aa-1,log(self.alpha))
		
		delta = tol + 1.0
	        iter = 0
		while abs(delta) > tol and iter < maxiter:
		    iter += 1
		    iC = self.calc_iC()
                    rhs = self.calc_rhs()

                    if len(self.ineqs) > 0 and quad_prog != None:
			print "        Using constrained solve."
                        # solve with inequality constraints
                        if abs(self.constraints).max() < 1e-10:
			  dtheta = quad_prog(iC, -rhs, \
                                        G = -self.ineqs, \
                                        h = dot(self.ineqs, self.theta0))
                        else:
			  dtheta = quad_prog(iC, -rhs, \
                                        G = -self.ineqs, \
                                        h = dot(self.ineqs, self.theta0), \
					A = self.constraints, \
					b = zeros(len(self.constraints)))
			  # assumes A . theta0 = 0
                    else: # We have to ignore ineqs! (can't linearly resolve)
                        if len(self.constraints) > 0:
                            iC = dot(dot(self.span, iC), self.span.transpose())
                            dtheta = dot(self.span.transpose(),
                                    la.solve(iC, dot(self.span, rhs)))
                        else:
                            dtheta = la.solve(iC, rhs)

		    # Re-calculate if > 10% difference.
		    if sum(abs(dtheta)) > 0.1*sum(abs(self.theta0)):
			print "Resetting theta0."
			self.set_theta0(self.theta0 + dtheta)
			self.dtheta = dtheta*0.0
		    else:
			self.dtheta = dtheta

		    # Conj-gradient minimize all nonlinear forces.
		    # Is it stable? We don't know.
		    if self.do_nonlin:
			acc = 0
			for name in self.nonlin.keys():
			    res, rj_jj = self.resid_nonlin(name)
			    #sol = newton_krylov(jac, self.nonlin[name][0], \
				#		method='lgmres', verbose=1)
			    #sol = fmin_cg(res, self.nonlin[name][0], jac)
			    #sol = least_squares(res, self.nonlin[name][0],
			#		        jac=jac, method='lq',
			#			ftol=1e-08,
			#			xtol=1e-10,
			#			gtol=1e-5,
			#			#x_scale='jac'
			#			)
			    self.nonlin[name][0] = lm_lstsq(
					self.nonlin[name][0], res, rj_jj)
			self.recompute_lin()
		    # test code!
		    #name="es"
		    #test_this_shiz(self.nonlin[name][0], *self.resid_nonlin(name))
		    
		    az, ibz, aa, iba = self.calc_za_ab()
		    self.z = (az-1.0)/ibz
		    self.alpha = (aa-1.0)/iba
		    
		    lp = dot(ibz,self.z) - dot(az-1,log(self.z)) \
			+ dot(iba,self.alpha) - dot(aa-1,log(self.alpha))
		    delta = llp-lp
		    llp = lp
		    print("  Iteration %d, delta = %e"%(iter,delta))
                    print("  Force Residuals (RMS err per atom type) =")
                    print("\n".join("    %5s %e"%(t,e) for (t,e)
                          in zip(self.type_names, sqrt((ibz-0.5*self.E0)/az))))
		
		dtheta, dmu2, df2 = self.calc_theta_stats()
		df2 = array(df2)/( self.S*3.0*self.pdb.atoms )
		self.df2 = df2
	
	def calc_penalty(self):
            pen = zeros(self.topol.hyp_params)
            for i,P in self.topol.prior:
                r0 = self.ind[i]
                r1 = self.ind[i+1]
		t = self.theta0[r0:r1] + self.dtheta[r0:r1]
                pen[i] = dot(dot(P, t), t)
            return pen*(pen > 0.0) # Forces negative pen -> 0
	
	# Note: const(D)-log(P) = dot(bz,self.z) - dot(az-1,log(self.z))\ 
	#                + dot(ba,self.alpha) - dot(aa-1,log(self.alpha))
	def calc_za_ab(self):
            #self.ft2 = dot(dot(self.D2,self.dtheta), self.dtheta)
            #bz = (self.ft2+self.F2) - 2*dot(self.DF, self.dtheta)
            bz = self.F2 + dot(dot(self.D2,self.dtheta)-2*self.DF, \
							self.dtheta)
            bz = 0.5*(bz*(bz > 0.0) + self.E0)
            
            ba = 0.5*(self.calc_penalty()+self.E0)
            return 1.5*self.nt*self.S, bz, 0.5*array(self.topol.pri_rank), ba
		
        def calc_rhs(self): # weight residual by atom type
	    return dot(self.z, self.DF) + self.rhs0

	def calc_iC(self):
		iC = tensordot(self.z, self.D2, axes=[0,0]) + self.iC0
		# Add in constraints.
		iC += self.calpha*dot(transpose(self.constraints), \
					self.constraints)
		# Add in prior info.
                for a, (i,P) in zip(self.alpha, self.topol.prior):
                    r0 = self.ind[i]
                    r1 = self.ind[i+1]
                    iC[r0:r1, r0:r1] += a * P
		return iC
	
	# Generate conditional samples.
	def update_sample(self, n=0, logalpha=None):
	    for step in xrange(n):
		#print "    Updating."
		iC = self.calc_iC()
                b = zeros((len(self.span),2))
                b[:,0] = rand.standard_normal(len(b)) # Sample
                b[:,1] = dot(self.span, self.calc_rhs()) # Mean
                if len(self.constraints) > 0:
                    iC = dot(dot(self.span, iC), self.span.transpose())
		try:
                    L = la.cholesky(iC)
                    b[:,1:] = forward_subst(L, b[:,1:])
                    b = back_subst(transpose(L), b)
		except la.linalg.LinAlgError:
                    w, A = self.dimensionality()
                    raise RuntimeError, "Force design matrix is degenerate!"
                    hC = dot(A, transpose(A)/sqrt(w)[:,newaxis])
                    b = dot(hC, b)
                    b[:,1] = dot(hC, b[:,1])
                mean = dot(self.span.transpose(), b[:,1])
                dtheta = dot(self.span.transpose(), b[:,0]+b[:,1])
                tries = 0
                while sum(dot(self.ineqs, dtheta + self.theta0) < 0.0) > 0\
                                                        and tries < 1000:
                    b[:,0:1] = back_subst(transpose(L),
                                  rand.standard_normal(len(b))[:,newaxis])
                    dtheta = dot(self.span.transpose(), b[:,0]+b[:,1])
                    tries += 1
                if tries == 1000:
                    print "rejected 1000 Gaussian samples!"
                else:
                    self.mean = mean
                    self.dtheta = dtheta
		
		az, ibz, aa, iba = self.calc_za_ab()
		self.z = array([rand.gamma(ai,1.0) for ai in az])/ibz
		self.alpha = array([rand.gamma(ai,1.0) for ai in aa])/iba
		if logalpha != None:
		    write_matrix(logalpha, reshape(self.alpha, (1,-1)), 'a')
		
	def calc_theta_stats(self):
		iC = self.calc_iC()
		b = self.calc_rhs()
		try:
			#dtheta = la.solve(iC, b)
			C = la.inv(iC)
		except la.linalg.LinAlgError:
			w, A = self.dimensionality()
			raise RuntimeError, "Force design matrix is degenerate!"
			C = dot(A, transpose(A)/w[:,newaxis])
		dtheta = dot(C, b)
		#return dtheta, [trace(la.solve(iC, D2)) for D2 in self.D2]
		fv = []
                for k,i in enumerate(self.ind[:-1]):
			ip = self.ind[k+1]
			D2t = sum(self.D2[:,i:ip,i:ip],0)
			fv.append(trace(dot(C[i:ip,i:ip],D2t)))
		return dtheta, [trace(dot(C, D2)) for D2 in self.D2], fv
	
	def sample(self, samples, skip=100, toss=10):
		if self.S < 1:
			raise ProgramError, "Error! no data has been collected!"
		if skip > 0:
			print "Doing sampling burn-in..."
		for i in xrange(skip):
			self.update_sample(toss)
		print "Collecting %d samples..."%samples
		if self.logalpha != None:
			out = open(self.logalpha, 'w')
			out.truncate()
			out.close()
		for i in xrange(samples):
			self.update_sample(toss, self.logalpha)
			dtheta, dmu2, df2 = self.calc_theta_stats()
			self.sum_dtheta.append(dtheta)
			self.sum_dmu2.append(dmu2)
			self.sum_df2.append(df2)
			self.sum_v.append(1.0/self.z)
			self.sum_alpha.append(self.alpha)
                        az, ibz, aa, iba = self.calc_za_ab()
                        self.sum_resid2.append((ibz-0.5*self.E0)/az)
			self.samples += 1
		self.posterior_estimate()
	
	# Best estimates from posterior distribution.
	def posterior_estimate(self):
		if self.samples < 1:
			raise ProgramError, "Error! No samples collected!"
		self.dtheta = sum(self.sum_dtheta,0)/self.samples
		dmu2 = sum(array(self.sum_dmu2), 0)
		df2 = sum(array(self.sum_df2), 0)
		for dmu in array(self.sum_dtheta)-self.dtheta:
			dmu2 += dot(dot(self.D2, dmu),dmu)
			f2 = []
                        for k,i in enumerate(self.ind[:-1]):
				ip = self.ind[k+1]
				D2t = sum(self.D2[:,i:ip,i:ip],0)
				f2.append(dot(dot(D2t, dmu[i:ip]), dmu[i:ip]))
			df2 += array(f2)
		dmu2 /= self.samples*self.S*3.0*self.nt
		df2 /= self.samples*self.S*3.0*sum(self.nt)
		v = sum(self.sum_v,0)/self.samples + dmu2
		self.df2 = df2
		#print "LINPROB = %e"%(float(sum(array(self.sum_E)<v*1e-3))\
		#	/ self.samples)
		#print "ERFAC = %e"%(self.ab0*2.0/v) # product of z and E0
		self.z = 1.0/v

        def write_out(self, name):
            # Dimensionalize and use topol's own write methods.
            write_topol(self.topol, name, (self.dtheta+self.theta0)*self.kT)

            if self.samples > 0:
                    avg_v = sum(self.sum_v,0)/self.samples
                    s_v = sum((array(self.sum_v)-avg_v)**2,0)
                    s_v = sqrt(s_v/self.samples)
            else:
                    avg_v = 1./self.z
                    s_v = zeros(len(self.z))
            az, ibz, aa, iba = self.calc_za_ab()
            resid2 = (ibz-0.5*self.E0)/az
            lam = open(path.join(name,"v.out"), 'w')
            lam.write("#type\tv\t<v>\tsigma_v\tR^2\n")
            for t,l2,avg,sd,res in zip(self.type_names, 1./self.z, \
                                       avg_v, s_v, resid2):
                    lam.write("%s\t%e\t%e\t%e\t%e\n"%(t,l2,avg,sd,res))
            lam.close()
            
            # Output force residuals per term (cheating by inspecting cg_topol)
            def get_names(t):
                if hasattr(t, "terms"):
                    return reduce(lambda a,b: a+get_names(b), t.terms, [])
                return [t.name]
            tname = get_names(self.topol)
            df = open(path.join(name,"df.out"), 'w')
            df.write("#type\t<stdev>\n")
            for df2, (i,P) in zip(self.df2, self.topol.prior):
                df.write( "%-16s %e\n"%(tname[i], sqrt(df2)) )
            df.close()
	
# Operates on row space of A
def orthonormalize(A, complete=True):
    U, s, V = la.svd(A)
    if any(abs(s) < 1e-8):
        print("Error: constraints are degenerate!")
    if complete:
        return V[:len(A)], V[len(A):]
    return V[:len(A)]

# Solve Ax=b, where A is lower diagonal.
def forward_subst(A, b):
	x = b.copy()
	for i in range(len(x)):
		x[i] = (x[i]-sum(A[i,:i,newaxis]*x[:i,...],0))/A[i,i]
	return x

# Solve Ax=b, where A is upper diagonal.
def back_subst(A, b):
	x = b.copy()
	for i in reversed(range(len(x))):
		x[i] = (x[i]-sum(A[i,i+1:,newaxis]*x[i+1:,...],0))/A[i,i]
	return x

def lm_lstsq(x0, res, rj_jj, tol=1e-5, stop = 1e-8, max_iter=50):
    err = res(x0)
    print "LM Initial error = %g"%err
    lm = 0.1 # tunable parameters
    v = 0.9
    delta = tol*2 + 1.0
    nup = 0
    ndn = 0
    while delta > tol and err > stop and nup + ndn < max_iter \
		    and nup-ndn < max_iter/10:
	rj, jj = rj_jj(x0)
	#print "    Diagonals: " + str(diag(jj))
	jj += diag(lm*diag(jj))
	x = x0 - la.solve(jj, rj)
	err2 = res(x)
	if err2 > err:
	    #print "    Error increased to %e"%err2
	    lm /= v
	    nup += 1
	else:
	    #print "    Error decreased to %e"%err2
	    lm *= v
	    ndn += 1
	    x0 = x
	    delta = err - err2
	    err = err2
    print "Ending lm optimization. ups = %d, downs = %d"%(nup, ndn)
    print "   Final error = %g"%err
    return x0

def test_this_shiz(x, res, jac):
    h = 1./(1./1e-6)
    r = res(x)
    der = jac(x)
    print r
    print der

    n = len(x)
    derp = zeros(der.shape)
    for i in range(n):
	r2 = res(x+h*identity(n)[i])
	derp[...,i] = (r2-r)/h
    print derp

