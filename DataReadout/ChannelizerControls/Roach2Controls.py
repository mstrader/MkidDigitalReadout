"""
Author:    Alex Walter
Date:      April 25, 2016
Firmware:  darkS2*.fpg

This class is for setting and reading LUTs, registers, and other memory components in the ROACH2 Virtex 6 FPGA using casperfpga tools.
It's also the IO for the ADC/DAC board's Virtex 7 FPGA through the ROACH2

NOTE: All freqencies are considered positive. A negative frequency can be asserted by the aliased signal of large positive frequency (by adding sample rate). This makes things easier for coding since I can check valid frequencies > 0 and also for calculating which fftBin a frequency resides in (see generateFftChanSelection()). 


Example usage:
    # Collect MKID info
    nFreqs=1024
    loFreq = 5.e9
    spacing = 2.e6
    freqList = np.arange(loFreq-nFreqs/2.*spacing,loFreq+nFreqs/2.*spacing,spacing)
    freqList+=np.random.uniform(-spacing,spacing,nFreqs)
    freqList = np.sort(freqList)
    attenList = np.random.randint(23,33,nFreqs)
    
    # Talk to Roach
    roach_0 = FpgaControls(ip, params, True, True)
    roach_0.setLOFreq(loFreq)
    roach_0.generateResonatorChannels(freqList)
    roach_0.generateFftChanSelection()
    roach_0.generateDacComb(freqList=None, resAttenList=attenList, globalDacAtten=17)
    roach_0.generateDdsTones()
    
    roach_0.loadChanSelection()
    roach_0.loadDacLUT()




List of Functions:
    __init__ -                      Connects to Roach2 FPGA, sets the delay between the dds lut and the end of the fft block
    connect -                       Connect to V6 FPGA on Roach2
    loadDdsShift -                  Set the delay between the dds lut and the end of the fft block
    generateResonatorChannels -     Figures out which stream:channel to assign to each resonator frequency
    generateFftChanSelection -      Assigns fftBins to each steam:channel
    loadSingleChanSelection -       Loads a channel for each stream into the channel selector LUT
    loadChanSelection -             Loops through loadSingleChanSelection()
    setLOFreq -                     Defines LO frequency as an attribute, self.LOFreq
    generateTones -                 Returns a list of I,Q time series for each frequency provided
    generateDacComb -               Returns a single I,Q time series representing the DAC freq comb
    loadDacLut -                    Loads the freq comb from generateDacComb() into the LUT
    generateDdsTones -              Defines interweaved tones for dds
    loadDdsLUT -                    Loads dds tones into Roach2 memory
    

    
List of useful class attributes:
    ip -                            ip address of roach2
    params -                        Dictionary of parameters
    freqList -                      List of resonator frequencies
    attenList -                     List of resonator attenuations
    freqChannels -                  2D array of frequencies. Each column is the a stream and each row is a channel. 
                                    If uneven number of frequencies this array is padded with -1's
    fftBinIndChannels -             2D array of fftBin indices corresponding to the frequencies/streams/channels in freqChannels. freq=-1 maps to fftBin=0.
    dacPhaseList -                  List of the most recent relative phases used for generating DAC frequency comb
    dacScaleFactor -                Scale factor for frequency comb to scale the sum of tones onto the DAC's dynamic range. 
                                    Careful, this is 1/scaleFactor we defined for ARCONS templar
    dacQuantizedFreqList -          List of frequencies used to define DAC frequency comb. Quantized to DAC digital limits
    dacFreqComb -                   Complex time series signal used for DAC frequency comb. 
    LOFreq -                        LO frequency of IF board
    ddsQuantizedFreqList -          2D array of frequencies shaped like freqChannels. Quantized to dds digital limits
    ddsPhaseList -                  2D array of frequencies shaped like freqChannels. Used to rotate loops.
    


TODO:
    uncomment self.fpgs, DDS Shift in __init__
    uncomment register writes in loadSingleChanSelection()
    uncomment register writes in loadDacLut()
    add code for setting LO freq in loadLOFreq()
    write code collecting data from ADC
    
    calibrate dds qdr with SDR/Projects/FirmwareTests/darkDebug/calibrate.py
    fix bug that doesn't work with less than 4 tones
"""

import sys,os,time,struct,math
import warnings, inspect
import matplotlib.pyplot as plt
import numpy as np
import scipy.special
import casperfpga
from readDict import readDict       #Part of the ARCONS-pipeline/util

class Roach2Controls:

    def __init__(self, ip, paramFile, verbose=False, debug=False):
        '''
        Input:
            ip - ip address string of ROACH2
            paramFile - param object or directory string to dictionary containing important info
            verbose - show print statements
            debug - Save some things to disk for debugging
        '''
        #np.random.seed(1) #Make the random phase values always the same
        
        self.verbose=verbose
        self.debug=debug
        
        self.ip = ip
        try:
            self.params = readDict()             
            self.params.readFromFile(paramFile)
        except TypeError:
            self.params = paramFile
        
        if debug and not os.path.exists(self.params['debugDir']):
            os.makedirs(self.params['debugDir']) 

        
        #Some more parameters
        self.freqPadValue = -1      # pad frequency lists so that we have a multiple of number of streams
        self.fftBinPadValue = 0     # pad fftBin selection with fftBin 0
        self.ddsFreqPadValue = -1   # 
        self.v7_ready = 0
        self.lut_dump_buffer_size = self.params['lut_dump_buffer_size']
    
    def connect(self):
        self.fpga = casperfpga.katcp_fpga.KatcpFpga(self.ip,timeout=50.)
        time.sleep(1)
        if not self.fpga.is_running():
            print 'Firmware is not running. Start firmware, calibrate, and load wave into qdr first!'
    
        self.fpga.get_system_information()
    
    def loadDdsShift(self,ddsShift=(75+256)):
        #set the delay between the dds lut and the end of the fft block (firmware dependent)
        self.fpga.write_int(self.params['ddsShift_reg'],ddsShift)
    
    def initializeV7UART(self, baud_rate = None, lut_dump_buffer_size = None):
        '''
        Initializes the UART connection to the Virtex 7.  Puts the V7 in Recieve mode, sets the 
        baud rate
        Defines global variables:
            self.baud_rate - baud rate of UART connection
            self.v7_ready - 1 when v7 is ready for a command
            self.lut_dump_data_period - number of clock cycles between writes to the UART
            self.lut_dump_buffer_size - size, in bytes, of each BRAM dump
        '''
        if(baud_rate == None):
            self.baud_rate = self.params['baud_rate']
        else:
            self.baud_rate = baud_rate
        
        if(lut_dump_buffer_size == None):
            self.lut_dump_buffer_size = self.params['lut_dump_buffer_size']
        else:
            self.lut_dump_buffer_size = lut_dump_buffer_size
        
        self.lut_dump_data_period = (10*self.params['fpgaClockRate'])//self.baud_rate + 1 #10 bits per data byte
        self.v7_ready = 0
        
        self.fpga.write_int(self.params['enBRAMDump_reg'], 0)
        self.fpga.write_int(self.params['txEnUART_reg'],0)
        self.fpga.write_int('a2g_ctrl_lut_dump_data_period', self.lut_dump_data_period)
        
        self.fpga.write_int(self.params['resetUART_reg'],1)
        time.sleep(1)
        self.fpga.write_int(self.params['resetUART_reg'],0)
        
        #while(not(self.v7_ready)):
        #    self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
        
        self.v7_ready = 0
        self.fpga.write_int(self.params['inByteUART_reg'],1) # Acknowledge that ROACH2 knows MB is ready for commands
        time.sleep(0.01)
        self.fpga.write_int(self.params['txEnUART_reg'],1)
        time.sleep(0.01)
        self.fpga.write_int(self.params['txEnUART_reg'],0)
    
    def initV7MB(self):
        """
        Send commands over UART to initialize V7.
        Call initializeV7UART first
        """
        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
        self.v7_ready = 0
        sendUARTCommand(self.params['mbEnableDACs'])
        
        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
        self.v7_ready = 0
        sendUARTCommand(self.params['mbSendLUTToDAC'])
        
        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
        self.v7_ready = 0
        sendUARTCommand(self.params['mbInitLO'])
        
        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
        self.v7_ready = 0
        sendUARTCommand(self.params['mbInitAtten'])

        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
        self.v7_ready = 0
        sendUARTCommand(self.params['mbEnFracLO'])
        
    def generateDdsTones(self, freqChannels=None, fftBinIndChannels=None, phaseList=None):
        """
        Create and interweave dds frequencies
        
        Call setLOFreq(), generateResonatorChannels(), generateFftChanSelection() first.
        
        INPUT:
            freqChannels - Each column contains the resonantor frequencies in a single stream. The row index is the channel number. It's padded with -1's. 
                           Made by generateResonatorChannels(). If None, use self.freqChannels
            fftBinIndChannels - Same shape as freqChannels but contains the fft bin index. Made by generateFftChanSelection(). If None, use self.fftBinIndChannels
            phaseList - Same shape as freqChannels. Contains phase offsets (0 to 2Pi) for dds sampling. If None, set all to zero
        
        OUTPUT:
            dictionary with following keywords
            'iStreamList' - 2D array. Each row is an interweaved list of i values for a single stream. 
            'qStreamList' - q values
            'quantizedFreqList' - 2d array of dds frequencies. (same shape as freqChannels) Padded with self.ddsFreqPadValue
            'phaseList' - 2d array of phases for each frequency (same shape as freqChannels) Padded with 0's
        """
        #Interpret Inputs
        if freqChannels is None:
            freqChannels = self.freqChannels
        if len(np.ravel(freqChannels))>self.params['nChannels']:
            raise ValueError("Too many freqs provided. Can only accommodate "+str(self.params['nChannels'])+" resonators")
        self.freqChannels = freqChannels
        if fftBinIndChannels is None:
            fftBinIndChannels = self.fftBinIndChannels
        if len(np.ravel(fftBinIndChannels))>self.params['nChannels']:
            raise ValueError("Too many freqs provided. Can only accommodate "+str(self.params['nChannels'])+" resonators")
        self.fftBinIndChannels = fftBinIndChannels
        if phaseList is None:
            phaseList = np.zeros(np.asarray(freqChannels).shape)
    
        if not hasattr(self,'LOFreq'):
            raise ValueError("Need to set LO freq by calling setLOFreq()")
        
        if self.verbose:
            print "Generating Dds Tones..."
        # quantize resonator tones to dds resolution
        # first figure out the actual frequencies being made by the DAC
        dacFreqList = freqChannels-self.LOFreq
        dacFreqList[np.where(dacFreqList<0.)] += self.params['dacSampleRate']  #For +/- freq
        dacFreqResolution = self.params['dacSampleRate']/(self.params['nDacSamplesPerCycle']*self.params['nLutRowsToUse'])
        dacQuantizedFreqList = np.round(dacFreqList/dacFreqResolution)*dacFreqResolution
        # Figure out how the dac tones end up relative to their FFT bin centers
        fftBinSpacing = self.params['dacSampleRate']/self.params['nFftBins']
        fftBinCenterFreqList = fftBinIndChannels*fftBinSpacing
        ddsFreqList = dacQuantizedFreqList - fftBinCenterFreqList
        # Quantize to DDS sample rate and make sure all freqs are positive by adding sample rate for aliasing
        ddsSampleRate = self.params['nDdsSamplesPerCycle'] * self.params['fpgaClockRate'] / self.params['nCyclesToLoopToSameChannel']
        ddsFreqList[np.where(ddsFreqList<0)]+=ddsSampleRate     # large positive frequencies are aliased back to negative freqs
        nDdsSamples = self.params['nDdsSamplesPerCycle']*self.params['nQdrRows']/self.params['nCyclesToLoopToSameChannel']
        ddsFreqResolution = 1.*ddsSampleRate/nDdsSamples
        ddsQuantizedFreqList = np.round(ddsFreqList/ddsFreqResolution)*ddsFreqResolution
        ddsQuantizedFreqList[np.where(freqChannels<0)] = self.ddsFreqPadValue     # Pad excess frequencies with -1
        self.ddsQuantizedFreqList = ddsQuantizedFreqList
        
        # For each Stream, generate tones and interweave time streams for the dds time multiplexed multiplier
        nStreams = int(self.params['nChannels']/self.params['nChannelsPerStream'])        #number of processing streams. For Gen 2 readout this should be 4
        iStreamList = []
        qStreamList = []
        for i in range(nStreams):
            # generate individual tone time streams
            toneParams={
                'freqList': ddsQuantizedFreqList[:,i][np.where(dacQuantizedFreqList[:,i]>0)],
                'nSamples': nDdsSamples,
                'sampleRate': ddsSampleRate,
                'amplitudeList': None,  #defaults to 1
                'phaseList': phaseList[:,i][np.where(dacQuantizedFreqList[:,i]>0)]}
            toneDict = self.generateTones(**toneParams)
            
            #scale amplitude to number of bits in memory and round
            nBitsPerSampleComponent = self.params['nBitsPerDdsSamplePair']/2
            maxValue = int(np.round(2**(nBitsPerSampleComponent - 1)-1))       # 1 bit for sign
            iValList = np.array(np.round(toneDict['I']*maxValue),dtype=np.int)
            qValList = np.array(np.round(toneDict['Q']*maxValue),dtype=np.int)
            
            #print 'iVals: '+str(iValList)
            #print 'qVals: '+str(qValList)
            #print np.asarray(iValList).shape
            
            
            #interweave the values such that we have two samples from freq 0 (row 0), two samples from freq 1, ... to freq 256. Then have the next two samples from freq 1 ...
            freqPad = np.zeros((self.params['nChannelsPerStream'] - len(toneDict['quantizedFreqList']),nDdsSamples),dtype=np.int)
            #First pad with missing resonators
            if len(iValList) >0:
                iValList = np.append(iValList,freqPad,0)    
                qValList = np.append(qValList,freqPad,0)
            else: #if no resonators in stream then everything is 0's
                iValList = freqPad
                qValList = freqPad
            iValList = np.reshape(iValList,(self.params['nChannelsPerStream'],-1,self.params['nDdsSamplesPerCycle']))
            qValList = np.reshape(qValList,(self.params['nChannelsPerStream'],-1,self.params['nDdsSamplesPerCycle']))
            iValList = np.swapaxes(iValList,0,1)
            qValList = np.swapaxes(qValList,0,1)
            iValues = iValList.flatten('C')
            qValues = qValList.flatten('C')
            
            # put into list
            iStreamList.append(iValues)
            qStreamList.append(qValues)
            phaseList[:len(toneDict['phaseList']),i] = toneDict['phaseList']    # We need this if we let self.generateTones() choose random phases
        
        self.ddsPhaseList = phaseList
        self.ddsIStreamsList = iStreamList
        self.ddsQStreamsList = qStreamList
        
        if self.verbose:
            print '\tDDS freqs: '+str(self.ddsQuantizedFreqList)
            for i in range(nStreams):
                print '\tStream '+str(i)+' I vals: '+str(self.ddsIStreamsList[i])
                print '\tStream '+str(i)+' Q vals: '+str(self.ddsQStreamsList[i])
            print '...Done!'
        
        return {'iStreamList':iStreamList, 'qStreamList':qStreamList, 'quantizedFreqList':ddsQuantizedFreqList, 'phaseList':phaseList}
    
    
    def loadDdsLUT(self, ddsToneDict=None):
        '''
        Load dds tones to LUT in Roach2 memory
        
        INPUTS:
            ddsToneDict - from generateDdsTones()
                dictionary with following keywords
                'iStreamList' - 2D array. Each row is an interweaved list of i values for a single stream. Columns are different streams.
                'qStreamList' - q values
        OUTPUTS:
            allMemVals - memory values written to QDR
        '''
        if ddsToneDict is None:
            try:
                ddsToneDict={'iStreamList':self.ddsIStreamsList,'qStreamList':self.ddsQStreamsList}
            except AttributeError:
                print "Need to run generateDdsTones() first!"
                raise
            
        memNames = self.params['ddsMemName_regs']
        allMemVals=[]
        for iMem in range(len(memNames)):
            iVals,qVals = ddsToneDict['iStreamList'][iMem],ddsToneDict['qStreamList'][iMem]
            memVals = self.formatWaveForMem(iVals,qVals,nBitsPerSamplePair=self.params['nBitsPerDdsSamplePair'],
                                            nSamplesPerCycle=self.params['nDdsSamplesPerCycle'],nMems=len(memNames),
                                            nBitsPerMemRow=self.params['nBytesPerQdrSample']*8,earlierSampleIsMsb=True)
            #time.sleep(.1)
            allMemVals.append(memVals)
            
            self.writeQdr(memNames[iMem], valuesToWrite=memVals[:,0], start=0, bQdrFlip=True, nQdrRows=self.params['nQdrRows'])
            
        return allMemVals
    
    def writeBram(self, memName, valuesToWrite, start=0, nRows=2**10):
        """
        format values and write them to bram
        
        
        """
        nBytesPerSample = 8
        formatChar = 'Q'
        memValues = np.array(valuesToWrite,dtype=np.uint64) #cast signed values
        nValues = len(valuesToWrite)
        toWriteStr = struct.pack('>{}{}'.format(nValues,formatChar),*memValues)
        self.fpga.blindwrite(memName,toWriteStr,start)
        
    def writeQdr(self, memName, valuesToWrite, start=0, bQdrFlip=True, nQdrRows=2**20):
        """
        format and write 64 bit values to qdr
        
        INPUTS:
        """
        nBytesPerSample = 8
        formatChar = 'Q'
        memValues = np.array(valuesToWrite,dtype=np.uint64) #cast signed values
        nValues = len(valuesToWrite)
        if bQdrFlip: #For some reason, on Roach2 with the current qdr calibration, the 64 bit word seen in firmware
            #has the first and second 32 bit chunks swapped compared to the 64 bit word sent by katcp, so to accommodate
            #we swap those chunks here, so they will be in the right order in firmware
            mask32 = int('1'*32,2)
            memValues = (memValues >> 32)+((memValues & mask32) << 32)
            #Unfortunately, with the current qdr calibration, the addresses in katcp and firmware are shifted (rolled) relative to each other
            #so to compensate we roll the values to write here
            memValues = np.roll(memValues,-1)
        toWriteStr = struct.pack('>{}{}'.format(nValues,formatChar),*memValues)
        self.fpga.blindwrite(memName,toWriteStr,start)
    
    def formatWaveForMem(self, iVals, qVals, nBitsPerSamplePair=32, nSamplesPerCycle=4096, nMems=3, nBitsPerMemRow=64, earlierSampleIsMsb=False):
        """
        put together IQ values from tones to be loaded to a firmware memory LUT
        
        INPUTS:
            iVals - time series of I values
            qVals - 
            
        """
        nBitsPerSampleComponent = nBitsPerSamplePair / 2
        #I vals and Q vals are 12 bits, combine them into 24 bit vals
        iqVals = (iVals << nBitsPerSampleComponent) + qVals
        iqRows = np.reshape(iqVals,(-1,nSamplesPerCycle))
        #we need to set dtype to object to use python's native long type
        colBitShifts = nBitsPerSamplePair*(np.arange(nSamplesPerCycle,dtype=object))
        if earlierSampleIsMsb:
            #reverse order so earlier (more left) columns are shifted to more significant bits
            colBitShifts = colBitShifts[::-1]
        
        iqRowVals = np.sum(iqRows<<colBitShifts,axis=1) #shift each col by specified amount, and sum each row
        #Now we have 2**20 row values, each is 192 bits and contain 8 IQ pairs 
        #next we divide these 192 bit rows into three 64-bit qdr rows

        #Mem0 has the most significant bits
        memRowBitmask = int('1'*nBitsPerMemRow,2)
        memMaskShifts = nBitsPerMemRow*np.arange(nMems,dtype=object)[::-1]
        #now do bitwise_and each value with the mask, and shift back down
        memRowVals = (iqRowVals[:,np.newaxis] >> memMaskShifts) & memRowBitmask

        #now each column contains the 64-bit qdr values to be sent to a particular qdr
        return memRowVals
    
    def loadDacLUT(self, combDict=None):
        """
        Sends frequency comb to V7 over UART, where it is loaded 
        into a lookup table
        
        Call generateDacComb() first
        
        INPUTS:
            combDict - return value from generateDacComb(). If None, it trys to gather information from attributes
        """
        if combDict is None:
            try:
                combDict = {'I':np.real(self.dacFreqComb).astype(np.int), 'Q':np.imag(self.dacFreqComb).astype(np.int), 'quantizedFreqList':self.dacQuantizedFreqList}
            except AttributeError:
                print "Run generateDacComb() first!"
                raise

        #Format comb for onboard memory
        #Interweave I and Q arrays
        memVals = np.empty(combDict['I'].size + combDict['Q'].size)
        memVals[0::2]=combDict['Q']
        memVals[1::2]=combDict['I']
        
        if self.debug:
            np.savetxt(self.params['debugDir']+'dacFreqs.txt', combDict['quantizedFreqList']/10**6., fmt='%3.11f', header="Array of DAC frequencies [MHz]")
        
        #Write data to LUTs
        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
        
        self.v7_ready = 0
        self.fpga.write_int(self.params['inByteUART_reg'],self.params['mbRecvDACLUT'])
        time.sleep(0.01)
        self.fpga.write_int(self.params['txEnUART_reg'],1)
        time.sleep(0.01)
        self.fpga.write_int(self.params['txEnUART_reg'],0)
        time.sleep(0.01)
        #time.sleep(10)
        self.fpga.write_int(self.params['enBRAMDump_reg'],1)

        
        print 'v7 ready before dump: ' + str(self.fpga.read_int(self.params['v7Ready_reg']))
        
        num_lut_dumps = int(math.ceil(len(memVals)*2/self.lut_dump_buffer_size)) #Each value in memVals is 2 bytes
        print 'num lut dumps ' + str(num_lut_dumps)
        print 'len(memVals) ' + str(len(memVals))

        sending_data = 1 #indicates that ROACH2 is still sending LUT
               
        for i in range(num_lut_dumps):
            if(len(memVals)>self.lut_dump_buffer_size/2*(i+1)):
                iqList = memVals[self.lut_dump_buffer_size/2*i:self.lut_dump_buffer_size/2*(i+1)]
            else:
                iqList = memVals[self.lut_dump_buffer_size/2*i:len(memVals)]
            
            iqList = iqList.astype(np.int16)
            toWriteStr = struct.pack('<{}{}'.format(len(iqList), 'h'), *iqList)
            print 'To Write Str Length: ', str(len(toWriteStr))
            print iqList.dtype
            print iqList
            print 'bram dump # ' + str(i)
            while(sending_data):
                sending_data = self.fpga.read_int(self.params['lutDumpBusy_reg'])
            self.fpga.blindwrite(self.params['lutBramAddr_reg'],toWriteStr,0)
            time.sleep(0.01)
            self.fpga.write_int(self.params['lutBufferSize_reg'],len(toWriteStr))
            time.sleep(0.01)
            
            while(not(self.v7_ready)):
                self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
            self.fpga.write_int(self.params['txEnUART_reg'],1)
            print 'enable write'
            time.sleep(0.05)
            self.fpga.write_int(self.params['txEnUART_reg'],0)
            sending_data = 1
            self.v7_ready = 0
            
        self.fpga.write_int(self.params['enBRAMDump_reg'],0)
        
        
    
    def setLOFreq(self,LOFreq):
        self.LOFreq = LOFreq
    
    def loadLOFreq(self,LOFreq=None):
        """
        Send LO frequency to V7 over UART.
        Must initialize LO first.
        
        INPUTS:
            LOFreq - LO frequency in MHz
        
        Sends LO freq one byte at a time, LSB first
           sends integer bytes first, then fractional
        """
        if LOFreq is None:
            try:
                LOFreq = self.LOFreq
            except AttributeError:
                print "Run setLOFreq() first!"
                raise
        self.LOFreq=LOFreq
        
        loFreqInt = int(LOFreq)
        loFreqFrac = LOFreq - loFreqInt
        
        # Put V7 into LO recv mode
        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])

        self.v7_ready = 0
        self.fpga.write_int(self.params['inByteUART_reg'],self.params['mbRecvLO'])
        time.sleep(0.01)
        self.fpga.write_int(self.params['txEnUART_reg'],1)
        time.sleep(0.01)
        self.fpga.write_int(self.params['txEnUART_reg'],0)        
        
        for i in range(2):
            transferByte = (loFreqInt>>(i*8))&255 #takes an 8-bit "slice" of loFreqInt
            
            while(not(self.v7_ready)):
                self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])

            self.v7_ready = 0
            self.fpga.write_int(self.params['inByteUART_reg'],transferByte)
            time.sleep(0.01)
            self.fpga.write_int(self.params['txEnUART_reg'],1)
            time.sleep(0.01)
            self.fpga.write_int(self.params['txEnUART_reg'],0)
        
        print 'loFreqFrac' + str(loFreqFrac)	
        loFreqFrac = int(loFreqFrac*(2**16))
        print 'loFreqFrac' + str(loFreqFrac)
        
        # same as transfer of int bytes
        for i in range(2):
            transferByte = (loFreqFrac>>(i*8))&255
            
            while(not(self.v7_ready)):
                self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])

            self.v7_ready = 0
            self.fpga.write_int(self.params['inByteUART_reg'],transferByte)
            time.sleep(0.01)
            self.fpga.write_int(self.params['txEnUART_reg'],1)
            time.sleep(0.01)
            self.fpga.write_int(self.params['txEnUART_reg'],0)
    
    def changeAtten(self, attenID, attenVal):
        """
        Change the attenuation on IF Board attenuators
        Must initialize attenuator SPI connection first
        INPUTS:
            attenID 
                1 - RF Upcoverter path
                2 - RF Upconverter path
                3 - RF Downconverter path
            attenVal - attenuation between 0 and 37.5 dB. Must be multiple of 0.25 dB
        """
        if attenVal > 31.75 or attenVal<0:
            raise ValueError("Attenuation must be between 0 and 31.75")
        
        attenVal = int(np.round(attenVal*4)) #attenVal register holds value 4x(attenuation)
        
        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
            
        self.v7_ready = 0
        self.sendUARTCommand(self.params['mbChangeAtten'])
        
        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
            
        self.v7_ready = 0
        self.sendUARTCommand(attenID)
        
        while(not(self.v7_ready)):
            self.v7_ready = self.fpga.read_int(self.params['v7Ready_reg'])
            
        self.v7_ready = 0
        self.sendUARTCommand(attenVal)
    
    def generateDacComb(self, freqList=None, resAttenList=None, globalDacAtten = 0, phaseList=None):
        """
        Creates DAC frequency comb by adding many complex frequencies together with specified amplitudes and phases.
        
        The resAttenList holds the absolute attenuation for each resonantor signal coming out of the DAC. Zero attenuation means that the tone amplitude is set to the full dynamic range of the DAC and the DAC attenuator(s) are set to 0. Thus, all values in resAttenList must be larger than globalDacAtten. If you decrease the globalDacAtten, the amplitude in the DAC LUT decreases so that the total attenuation of the signal is the same. 
        
        Note: Usually the attenuation values are integer dB values but technically the DAC attenuators can be set to every 1/4 dB and the amplitude in the DAC LUT can have arbitrary attenuation (quantized by number of bits).
        
        INPUTS:
            freqList - list of all resonator frequencies. If None, use self.freqList
            resAttenList - list of absolute attenuation values (dB) for each resonator. If None, use 20's
            globalDacAtten - global attenuation for entire DAC. Sum of the two DAC attenuaters on IF board
            dacPhaseList - list of phases for each complex signal. If None, generates random phases. Old phaseList is under self.dacPhaseList
            
        OUTPUTS:
            dictionary with keywords
            I - I(t) values for frequency comb [signed 32-bit integers]
            Q - Q(t)
            quantizedFreqList - list of frequencies after digitial quantiziation
        """
        # Interpret Inputs
        if freqList is None:
            freqList=self.freqList
        if len(freqList)>self.params['nChannels']:
            warnings.warn("Too many freqs provided. Can only accommodate "+str(self.params['nChannels'])+" resonators")
            freqList = freqList[:self.params['nChannels']]
        freqList = np.ravel(freqList).flatten()
        if resAttenList is None:
            warnings.warn("Individual resonator attenuations assumed to be 20")
            resAttenList=np.zeros(len(freqList))+20
        if len(resAttenList)>self.params['nChannels']:
            warnings.warn("Too many attenuations provided. Can only accommodate "+str(self.params['nChannels'])+" resonators")
            resAttenList = resAttenList[:self.params['nChannels']]
        resAttenList = np.ravel(resAttenList).flatten()
        if len(freqList) != len(resAttenList):
            raise ValueError("Need exactly one attenuation value for each resonant frequency!")
        if (phaseList is not None) and len(freqList) != len(phaseList):
            raise ValueError("Need exactly one phase value for each resonant frequency!")
        if np.any(resAttenList < globalDacAtten):
            raise ValueError("Impossible to attain desired resonator attenuations! Decrease the global DAC attenuation.")
        self.attenList = resAttenList
        self.freqList = freqList
        
        if self.verbose:
            print 'Generating DAC comb...'
        
        # Calculate relative amplitudes for DAC LUT
        nBitsPerSampleComponent = self.params['nBitsPerSamplePair']/2
        maxAmp = int(np.round(2**(nBitsPerSampleComponent - 1)-1))       # 1 bit for sign
        amplitudeList = maxAmp*10**(-(resAttenList - globalDacAtten)/20.)
        
        # Calculate nSamples and sampleRate
        nSamples = self.params['nDacSamplesPerCycle']*self.params['nLutRowsToUse']
        sampleRate = self.params['dacSampleRate']
        
        # Calculate resonator frequencies for DAC
        if not hasattr(self,'LOFreq'):
            raise ValueError("Need to set LO freq by calling setLOFreq()")
        dacFreqList = self.freqList-self.LOFreq
        dacFreqList[np.where(dacFreqList<0.)] += self.params['dacSampleRate']  #For +/- freq
        
        # Generate and add up individual tone time series.
        toneDict = self.generateTones(dacFreqList, nSamples, sampleRate, amplitudeList, phaseList)
        self.dacQuantizedFreqList = toneDict['quantizedFreqList']
        self.dacPhaseList = toneDict['phaseList']
        iValues = np.array(np.round(np.sum(toneDict['I'],axis=0)),dtype=np.int)
        qValues = np.array(np.round(np.sum(toneDict['Q'],axis=0)),dtype=np.int)
        self.dacFreqComb = iValues + 1j*qValues
        
        # check that we are utilizing the dynamic range of the DAC correctly
        highestVal = np.max((np.abs(iValues).max(),np.abs(qValues).max()))
        expectedHighestVal_sig = scipy.special.erfinv((len(iValues)-0.1)/len(iValues))*np.sqrt(2.)   # 10% of the time there should be a point this many sigmas higher than average
        if highestVal > expectedHighestVal_sig*np.max((np.std(iValues),np.std(qValues))):
            warnings.warn("The freq comb's relative phases may have added up sub-optimally. You should calculate new random phases")
        if highestVal > maxAmp:
            dBexcess = int(np.ceil(20.*np.log10(1.0*highestVal/maxAmp)))
            raise ValueError("Not enough dynamic range in DAC! Try decreasing the global DAC Attenuator by "+str(dBexcess)+' dB')
        elif 1.0*maxAmp/highestVal > 10**((1)/20.):
            # all amplitudes in DAC less than 1 dB below max allowed by dynamic range
            warnings.warn("DAC Dynamic range not fully utilized. Increase global attenuation by: "+str(int(np.floor(20.*np.log10(1.0*maxAmp/highestVal))))+' dB')
        
        if self.verbose:
            print '\tUsing '+str(1.0*highestVal/maxAmp*100)+' percent of DAC dynamic range'
            print '\thighest: '+str(highestVal)+' out of '+str(maxAmp)
            print '\tsigma_I: '+str(np.std(iValues))+' sigma_Q: '+str(np.std(qValues))
            print '\tLargest val_I: '+str(1.0*np.abs(iValues).max()/np.std(iValues))+' sigma. Largest val_Q: '+str(1.0*np.abs(qValues).max()/np.std(qValues))+' sigma.'
            print '\tExpected val: '+str(expectedHighestVal_sig)+' sigmas'
            print '\n\tDac freq list: '+str(self.dacQuantizedFreqList)
            print '\tDac Q vals: '+str(qValues)
            print '\tDac I vals: '+str(iValues)
            print '...Done!'

        
        if self.debug:
            plt.figure()
            plt.plot(iValues)
            plt.plot(qValues)
            std_i = np.std(iValues)
            std_q = np.std(qValues)
            plt.axhline(y=std_i,color='k')
            plt.axhline(y=2*std_i,color='k')
            plt.axhline(y=3*std_i,color='k')
            plt.axhline(y=expectedHighestVal_sig*std_i,color='r')
            plt.axhline(y=expectedHighestVal_sig*std_q,color='r')
            
            plt.figure()
            plt.hist(iValues,1000)
            plt.hist(qValues,1000)
            x_gauss = np.arange(-maxAmp,maxAmp,maxAmp/2000.)
            i_gauss = len(iValues)/(std_i*np.sqrt(2.*np.pi))*np.exp(-x_gauss**2/(2.*std_i**2.))
            q_gauss = len(qValues)/(std_q*np.sqrt(2.*np.pi))*np.exp(-x_gauss**2/(2.*std_q**2.))
            plt.plot(x_gauss,i_gauss)
            plt.plot(x_gauss,q_gauss)
            plt.axvline(x=std_i,color='k')
            plt.axvline(x=2*std_i,color='k')
            plt.axvline(x=3*std_i,color='k')
            plt.axvline(x=expectedHighestVal_sig*std_i,color='r')
            plt.axvline(x=expectedHighestVal_sig*std_q,color='r')
            
            plt.figure()
            sig = np.fft.fft(self.dacFreqComb)
            sig_freq = np.fft.fftfreq(len(self.dacFreqComb),1./self.params['dacSampleRate'])
            plt.plot(sig_freq, np.real(sig),'b')
            plt.plot(sig_freq, np.imag(sig),'g')
            for f in self.dacQuantizedFreqList:
                x_f=f
                if f > self.params['dacSampleRate']/2.:
                    x_f=f-self.params['dacSampleRate']
                plt.axvline(x=x_f, ymin=np.amin(np.real(sig)), ymax = np.amax(np.real(sig)), color='r')
            #plt.show()
            
        
        return {'I':iValues,'Q':qValues,'quantizedFreqList':self.dacQuantizedFreqList}
        
    
    def generateTones(self, freqList, nSamples, sampleRate, amplitudeList, phaseList):
        """
        Generate a list of complex signals with amplitudes and phases specified and frequencies quantized
        
        INPUTS:
            freqList - list of resonator frequencies
            nSamples - Number of time samples
            sampleRate - Used to quantize the frequencies
            amplitudeList - list of amplitudes. If None, use 1.
            phaseList - list of phases. If None, use random phase
        
        OUTPUTS:
            dictionary with keywords
            I - each element is a list of I(t) values for specific freq
            Q - Q(t)
            quantizedFreqList - list of frequencies after digitial quantiziation
            phaseList - list of phases for each frequency
        """
        if amplitudeList is None:
            amplitudeList = np.asarray([1.]*len(freqList))
        if phaseList is None:
            phaseList = np.random.uniform(0,2.*np.pi,len(freqList))
        if len(freqList) != len(amplitudeList) or len(freqList) != len(phaseList):
            raise ValueError("Need exactly one phase and amplitude value for each resonant frequency!")
        
        # Quantize the frequencies to their closest digital value
        freqResolution = sampleRate/nSamples
        quantizedFreqList = np.round(freqList/freqResolution)*freqResolution
        
        # generate each signal
        iValList = []
        qValList = []
        dt = 1. / sampleRate
        t = dt*np.arange(nSamples)
        for i in range(len(quantizedFreqList)):
            phi = 2.*np.pi*quantizedFreqList[i]*t
            expValues = amplitudeList[i]*np.exp(1.j*(phi+phaseList[i]))
            iValList.append(np.real(expValues))
            qValList.append(np.imag(expValues))
        
        '''
        if self.debug:
            plt.figure()
            for i in range(len(quantizedFreqList)):
                plt.plot(iValList[i])
                plt.plot(qValList[i])
            #plt.show()
        '''
        return {'I':np.asarray(iValList),'Q':np.asarray(qValList),'quantizedFreqList':quantizedFreqList,'phaseList':phaseList}
        
        
    def generateResonatorChannels(self, freqList,order='F'):
        """
        Algorithm for deciding which resonator frequencies are assigned to which stream and channel number.
        This is used to define the dds LUTs and calculate the fftBin index for each freq to set the appropriate chan_sel block
        
        Try to evenly distribute the given frequencies into each stream
        
        INPUTS:
            freqList - list of resonator frequencies (Assumed sequential but doesn't really matter)
            order - 'F' places sequential frequencies into a single stream
                    'C' places sequential frequencies into the same channel number
        OUTPUTS:
            self.freqChannels - Each column contains the resonantor frequencies in a single stream. The row index is the channel number. It's padded with -1's. 
        """
        #Interpret inputs...
        if order not in ['F','C','A']:  #if invalid, grab default value
            args,__,__,defaults = inspect.getargspec(Roach2Controls.generateResonatorChannels)
            order = defaults[args.index('order')-len(args)]
            if self.verbose: print "Invalid 'order' parameter for generateResonatorChannels(). Changed to default: "+str(order)
        if len(freqList)>self.params['nChannels']:
            warnings.warn("Too many freqs provided. Can only accommodate "+str(self.params['nChannels'])+" resonators")
            freqList = freqList[:self.params['nChannels']]
        self.freqList = np.ravel(freqList)
        self.freqChannels = self.freqList
        if self.verbose:
            print 'Generating Resonator Channels...'
        
        #Pad with freq = -1 so that freqChannels's length is a multiple of nStreams
        nStreams = int(self.params['nChannels']/self.params['nChannelsPerStream'])        #number of processing streams. For Gen 2 readout this should be 4
        padNum = (nStreams - (len(self.freqChannels) % nStreams))%nStreams  # number of empty elements to pad
        padValue = self.freqPadValue   #pad with freq=-1
        if order == 'F':
            for i in range(padNum):
                ind = len(self.freqChannels)-i*np.ceil(len(self.freqChannels)*1.0/nStreams)
                self.freqChannels=np.insert(self.freqChannels,int(ind),padValue)
        else:
            self.freqChannels = np.append(self.freqChannels, [padValue]*(padNum))
        
        #Split up to assign channel numbers
        self.freqChannels = np.reshape(self.freqChannels,(-1,nStreams),order)
        
        if self.verbose:
            print '\tFreq Channels: ',self.freqChannels
            print '...Done!'

        return self.freqChannels
        
        
        
    def generateFftChanSelection(self,freqChannels=None):
        '''
        This calculates the fftBin index for each resonant frequency and arranges them by stream and channel.
        Used by channel selector block
        Call setLOFreq() and generateResonatorChannels() first.
        
        INPUTS (optional):
            freqChannels - 2D array of frequencies where each column is the a stream and each row is a channel. If freqChannels isn't given then try to grab it from attribute. 
        
        OUTPUTS:
            self.fftBinIndChannels - Array with each column containing the fftbin index of a single stream. The row index is the channel number
            
        '''
        if freqChannels is None:
            try:
                freqChannels = self.freqChannels
            except AttributeError:
                print "Run generateResonatorChannels() first!"
                raise
        freqChannels = np.asarray(freqChannels)
        if self.verbose:
            print "Finding FFT Bins..."
        
        #The frequencies seen by the fft block are actually from the DAC, up/down converted by the IF board, and then digitized by the ADC
        dacFreqChannels = (freqChannels-self.LOFreq)
        dacFreqChannels[np.where(dacFreqChannels<0)]+=self.params['dacSampleRate']
        freqResolution = self.params['dacSampleRate']/(self.params['nDacSamplesPerCycle']*self.params['nLutRowsToUse'])
        dacQuantizedFreqChannels = np.round(dacFreqChannels/freqResolution)*freqResolution
        
        #calculate fftbin index for each freq
        binSpacing = self.params['dacSampleRate']/self.params['nFftBins']
        genBinIndex = dacQuantizedFreqChannels/binSpacing
        self.fftBinIndChannels = np.round(genBinIndex)
        self.fftBinIndChannels[np.where(freqChannels<0)]=self.fftBinPadValue      # empty channels have freq=-1. Assign this to fftBin=0
        
        self.fftBinIndChannels = self.fftBinIndChannels.astype(np.int)
        
        if self.verbose:
            print '\tfft bin indices: ',self.fftBinIndChannels
            print '...Done!'
        
        return self.fftBinIndChannels

        
    def loadChanSelection(self,fftBinIndChannels=None):
        """
        Loads fftBin indices to all channels (in each stream), to configure chan_sel block in firmware on self.fpga
        Call generateFftChanSelection() first

        
        INPUTS (optional):
            fftBinIndChannels - Array with each column containing the fftbin index of a single stream. The row is the channel number
        """
        if fftBinIndChannels is None:
            try:
                fftBinIndChannels = self.fftBinIndChannels
            except AttributeError:
                print "Run generateFftChanSelection() first!"
                raise
        
        if self.verbose: print 'Configuring chan_sel block...\n\tCh: Stream'+str(range(len(fftBinIndChannels[0])))
        for row in range(len(fftBinIndChannels)):
            if row > self.params['nChannelsPerStream']:
                warnings.warn("Too many freqs provided. Can only accommodate "+str(self.params['nChannels'])+" resonators")
                break
            self.loadSingleChanSelection(selBinNums=fftBinIndChannels[row],chanNum=row)
        if self.verbose: print '...Done!'
        if self.debug:
            np.savetxt(self.params['debugDir']+'freqChannels.txt', self.freqChannels/10**9.,fmt='%2.25f',header="2D Array of MKID frequencies [GHz]. \nEach column represents a stream and each row is a channel")
            np.savetxt(self.params['debugDir']+'fftBinIndChannels.txt', self.fftBinIndChannels,fmt='%8i',header="2D Array of fftBin Indices. \nEach column represents a stream and each row is a channel")
        
    def loadSingleChanSelection(self,selBinNums,chanNum=0):
        """
        Assigns bin numbers to a single channel (in each stream), to configure chan_sel block
        Used by loadChanSelection()

        INPUTS:
            selBinNums: array of bin numbers (for each stream) to be assigned to chanNum (4 element int array for Gen 2 firmware)
            chanNum: the channel number to be assigned
        """
        nStreams = int(self.params['nChannels']/self.params['nChannelsPerStream'])        #number of processing streams. For Gen 2 readout this should be 4
        if selBinNums is None or len(selBinNums) != nStreams:
            raise TypeError,'selBinNums must have number of elements matching number of streams in firmware'
        
        self.fpga.write_int(self.params['chanSelLoad_reg'],0) #set to zero so nothing loads while we set other registers.

        #assign the bin number to be loaded to each stream
        for i in range(nStreams):
            self.fpga.write_int(self.params['chanSel_regs'][i],selBinNums[i])
        time.sleep(.1)
        
        #in the register chan_sel_load, the lsb initiates the loading of the above bin numbers into memory
        #the 8 bits above the lsb indicate which channel is being loaded (for all streams)
        loadVal = (chanNum << 1) + 1
        self.fpga.write_int(self.params['chanSelLoad_reg'],loadVal)
        time.sleep(.1) #give it a chance to load

        self.fpga.write_int(self.params['chanSelLoad_reg'],0) #stop loading
        
        if self.verbose: print '\t'+str(chanNum)+': '+str(selBinNums)

    def startPhaseStream(self,selChanIndex=0, pktsPerFrame=100, fabric_port=50000, destIPID=50):
        """initiates streaming of phase timestream (after prog_fir) to the 1Gbit ethernet

        INPUTS:
            selChanIndex: which channel to stream
            pktsPerFrame: number of 8 byte photon words per ethernet frame
            fabric_port
            destIPID: destination IP is 10.0.0.destIPID
            
        """
        dest_ip = 0xa000000 + destIPID

        #configure the gbe core, 
        print 'restarting'
        self.fpga.write_int(self.params['destIP_reg'],dest_ip)
        self.fpga.write_int(self.params['fabricPort_reg'],fabric_port)
        self.fpga.write_int(self.params['wordsPerFrame_reg'],pktsPerFrame)
        #reset the core to make sure it's in a clean state
        self.fpga.write_int(self.params['gbe64Rst_reg'],1)
        time.sleep(.1)
        self.fpga.write_int(self.params['gbe64Rst_reg'],0)

        #choose what channel to stream
        self.fpga.write_int(self.params['phaseDumpChanSel_reg'],selChanIndex)
        #turn it on
        self.fpga.write_int(self.params['photonCapStart_reg'],0)#make sure we're not streaming photons
        self.fpga.write_int(self.params['phaseDumpEn_reg'],1)
    
    def stopStream(self):
        """stops streaming of phase timestream (after prog_fir) to the 1Gbit ethernet

        """
        fpga.write_int(self.params['phaseDumpEn_reg'],0)
    
    def recvPhaseStream(self, channel=0, duration=60, host = '10.0.0.50', port = 50000):
        """
        Recieves phase timestream data over ethernet, writes it to a file
        
        INPUTS:
            channel - channel number of incoming phase data
            duration - duration (in seconds) of phase stream
            host - IP address of computer receiving packets 
                (represented as a string)
            port
        
        """
        d = datetime.datetime.today()
        filename = ('phase_dump_pixel_' + str(channel) + '_' + str(d.day) + '_' + str(d.month) + '_' + 
            str(d.year) + '_' + str(d.hour) + '_' + str(d.minute) + str('.bin'))
        
        host = '10.0.0.50'
        port = 50000
        # create dgram udp socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error:
            print 'Failed to create socket'
            sys.exit()

        # Bind socket to local host and port
        try:
            sock.bind((host, port))
        except socket.error , msg:
            print 'Bind failed. Error Code : ' + str(msg[0]) + ' Message ' + msg[1]
            sys.exit()
        print 'Socket bind complete'

        bufferSize = int(800) #100 8-byte values
        iFrame = 0
        nFramesLost = 0
        lastPack = -1
        expectedPackDiff = -1
        frameData = ''

        dumpFile = open(filename, 'w')
        
        startTime = time.time()
        while (time.time()-startTime) < duration:
            frame = sock.recvfrom(bufferSize)
            frameData += frame[0]
            iFrame += 1

        print 'Exiting'
        sock.close()
        dumpFile.write(frameData)
        dumpFile.close()

        sock.close()
        dumpFile.close()
    
    def takePhaseStream(self, selChanIndex=0, duration=60, pktsPerFrame=100, fabric_port=50000, destIPID=50):
        """
        Takes phase timestream data from the specified channel for the specified amount of time
        
        INPUTS:
            selChanIndex: which channel to stream
            duration: duration (in seconds) of stream
            pktsPerFrame: number of 8 byte photon words per ethernet frame
            fabric_port
            destIPID: destination IP is 10.0.0.destIPID
                IP address of computer receiving stream
            
        """
        self.startPhaseStream(selChanIndex, pktsPerFrame, fabric_port, destIPID)
        self.recvPhaseStream(selChanIndex, duration, '10.0.0.'+str(destIPID), fabric_port)
        self.stopStream()
        
    def performIQSweep(self,startLOFreq,stopLOFreq,stepLOFreq):
        """
        Performs a sweep over the LO frequency.  Records 
        one IQ point per channel per freqeuency; stores in
        self.iqSweepData

        INPUTS:
            startLOFreq - starting sweep frequency
            stopLOFreq - final sweep frequency
            stepLOFreq - frequency sweep step size
        """
        
        LOFreqs = range(startLOFreq, stopLOFreq, stepLOFreq)
        iqData = np.array([])
        self.fpga.write_int(self.params['iqSnpStart_reg'],0)
        
        for freq in LOFreqs:
            self.loadLOFreq(freq)
            time.sleep(0.1)
            self.fpga.write_int(self.params['iqSnpStart_reg'],1)
            self.fpga.snapshots['darksc2_acc_iq_avg0'].arm(man_valid = False, man_trig = False)
            iqPt = self.fpga.snapshots['darksc2_acc_iq_avg0'].read(timeout = 10, arm = False)['data']
            iqData = np.append(iqData, iqPt['in_iq'])
            self.fpga.write_int(self.params['iqSnpStart_reg'],0)

        self.iqSweepData = iqData

    
    def sendUARTCommand(self, inByte):
        """
        Sends a single byte to V7 over UART
        Doesn't wait for a v7_ready signal
        Inputs:
            inByte - byte to send over UART
        """
        self.fpga.write_int(self.params['inByteUART_reg'],inByte)
        time.sleep(0.01)
        self.fpga.write_int(self.params['txEnUART_reg'],1)
        time.sleep(0.01)
        self.fpga.write_int(self.params['txEnUART_reg'],0)        
        
if __name__=='__main__':
    if len(sys.argv) > 1:
        ip = sys.argv[1]
    else:
        ip='10.0.0.112'
    if len(sys.argv) > 2:
        params = sys.argv[2]
    else:
        params='DarknessFpga_V2.param'
    print ip
    print params

    #warnings.filterwarnings('error')
    #freqList = [7.32421875e9, 8.e9, 9.e9, 10.e9,11.e9,12.e9,13.e9,14.e9,15e9,16e9,17.e9,18.e9,19.e9,20.e9,21.e9,22.e9,23.e9]
    nFreqs=170
    loFreq = 5.e9
    spacing = 2.e6
    freqList = np.arange(loFreq-nFreqs/2.*spacing,loFreq+nFreqs/2.*spacing,spacing)
    freqList+=np.random.uniform(-spacing,spacing,nFreqs)
    freqList = np.sort(freqList)
    attenList = np.random.randint(40,45,nFreqs)
    
    #freqList=np.asarray([5.2498416321e9, 5.125256256e9, 4.852323456e9, 4.69687416351e9])#,4.547846e9])
    #attenList=np.asarray([41,42,43,45])#,6])
    
    #freqList=np.asarray([5.12512345e9])
    #attenList=np.asarray([0])
    
    #attenList = attenList[np.where(freqList > loFreq)]
    #freqList = freqList[np.where(freqList > loFreq)]
    
    roach_0 = Roach2Controls(ip, params, True, True)
    #roach_0.connect()
    roach_0.setLOFreq(loFreq)
    roach_0.generateResonatorChannels(freqList)
    roach_0.generateFftChanSelection()
    roach_0.generateDacComb(resAttenList=attenList,globalDacAtten=9)
    #roach_0.generateDdsTones()
    roach_0.debug=False
    for i in range(10000):
        
        roach_0.generateDacComb(resAttenList=attenList,globalDacAtten=9)
    
    #roach_0.loadDdsLUT()
    #roach_0.loadChanSelection()
    #roach_0.initializeV7UART()
    #roach_0.loadDacLUT()
    
    
    
    #roach_0.generateDacComb(freqList, attenList, 17)
    #print roach_0.phaseList
    #print 10**(-0.25/20.)
    #roach_0.generateDacComb(freqList, attenList, 17, phaseList = roach_0.phaseList, dacScaleFactor=roach_0.dacScaleFactor*10**(-3./20.))
    #roach_0.generateDacComb(freqList, attenList, 20, phaseList = roach_0.phaseList, dacScaleFactor=roach_0.dacScaleFactor)
    #roach_0.loadDacLUT()
    
    #roach_0.generateDdsTones()
    #if roach_0.debug: plt.show()
    
