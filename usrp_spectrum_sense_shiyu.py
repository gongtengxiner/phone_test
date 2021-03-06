#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Copyright 2005,2007,2011 Free Software Foundation, Inc.
#
# This file is part of GNU Radio
#
# GNU Radio is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
#
# GNU Radio is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GNU Radio; see the file COPYING.  If not, write to
# the Free Software Foundation, Inc., 51 Franklin Street,
# Boston, MA 02110-1301, USA.
#
import numpy as np
import matplotlib.pyplot as plt
from gnuradio import gr, eng_notation
from gnuradio import blocks
from gnuradio import audio
from gnuradio import filter
from gnuradio import fft
from gnuradio import uhd
from gnuradio.eng_option import eng_option
from optparse import OptionParser
import sys
import math
import struct
import threading
from datetime import datetime
import time

sys.stderr.write("Warning:show_band must be smaller than samplerate,better be 3/4 of samplerate\n\n")

class ThreadClass(threading.Thread):
    def run(self):
        return

class tune(gr.feval_dd):
    """
    This class allows C++ code to callback into python.
    """
    def __init__(self, tb):
        gr.feval_dd.__init__(self)
        self.tb = tb

    def eval(self, ignore):
        """
        返回下一个中心频率，并延时0.1ms
        This method is called from blocks.bin_statistics_f when it wants
        to change the center frequency.  This method tunes the front
        end to the new center frequency, and returns the new frequency
        as its result.
        """

        try:
            # We use this try block so that if something goes wrong
            # from here down, at least we'll have a prayer of knowing
            # what went wrong.  Without this, you get a very
            # mysterious:
            #
            #   terminate called after throwing an instance of
            #   'Swig::DirectorMethodException' Aborted
            #
            # message on stderr.  Not exactly helpful ;)

            new_freq = self.tb.set_next_freq()

            # wait until msgq is empty before continuing
            while(self.tb.msgq.full_p()):
                #print "msgq full, holding.."
                time.sleep(0.1)

            return new_freq

        except Exception, e:
            print "tune: Exception: ", e


class parse_msg(object):
    def __init__(self, msg):
        self.center_freq = msg.arg1()
        self.vlen = int(msg.arg2())
        assert(msg.length() == self.vlen * gr.sizeof_float)

        # FIXME consider using NumPy array
        t = msg.to_string()
        self.raw_data = t
        self.data = struct.unpack('%df' % (self.vlen,), t)


class my_top_block(gr.top_block):

    def __init__(self):
        gr.top_block.__init__(self)

        # usage = "usage: %prog [options] min_freq max_freq"
        usage = "usage: %prog [options] mycenter_freq show_band"

        parser = OptionParser(option_class=eng_option, usage=usage)
        parser.add_option("-a", "--args", type="string", default="",
                          help="UHD device device address args [default=%default]")
        parser.add_option("", "--spec", type="string", default=None,
	                  help="Subdevice of UHD device where appropriate")
        parser.add_option("-A", "--antenna", type="string", default=None,
                          help="select Rx Antenna where appropriate")
        parser.add_option("-s", "--samp-rate", type="eng_float", default=1e6,
                          help="set sample rate [default=%default]")
        parser.add_option("-g", "--gain", type="eng_float", default=None,
                          help="set gain in dB (default is midpoint)")
        parser.add_option("", "--tune-delay", type="eng_float",
                          default=0.25, metavar="SECS",
                          help="time to delay (in seconds) after changing frequency [default=%default]")
        parser.add_option("", "--dwell-delay", type="eng_float",
                          default=0.25, metavar="SECS",
                          help="time to dwell (in seconds) at a given frequency [default=%default]")
        # parser.add_option("-b", "--channel-bandwidth", type="eng_float",
        #                   default=6.25e3, metavar="Hz",
        #                   help="channel bandwidth of fft bins in Hz [default=%default]")#这个应该是频率分辨度(的而他f)
        parser.add_option("-l", "--lo-offset", type="eng_float",
                          default=0, metavar="Hz",
                          help="lo_offset in Hz [default=%default]")
        parser.add_option("-q", "--squelch-threshold", type="eng_float",
                          default=None, metavar="dB",
                          help="squelch threshold in dB [default=%default]")
        parser.add_option("-F", "--fft-size", type="int", default=None,
                          help="specify number of FFT bins [default=1024]")
        parser.add_option("", "--real-time", action="store_true", default=False,
                          help="Attempt to enable real-time scheduling")

        (options, args) = parser.parse_args()
        if len(args) != 2:
            parser.print_help()
            sys.exit(1)

        # self.channel_bandwidth = options.channel_bandwidth

        myusrprate=options.samp_rate
        self.fft_size = options.fft_size
        self.channel_bandwidth = myusrprate/self.fft_size 

        self.mycenter_freq = eng_notation.str_to_num(args[0])
        self.show_band     = eng_notation.str_to_num(args[1])
        if self.show_band >myusrprate:
            sys.stderr.write("error:show_band must be smaller than samplerate\n")
            sys.exit(1)
        show_band=self.show_band 
        #add
        temp_varible=show_band/2
        self.min_freq = self.mycenter_freq - temp_varible
        self.max_freq = self.mycenter_freq + temp_varible

        args[0]=eng_notation.num_to_str(self.min_freq)
        args[1]=eng_notation.num_to_str(self.max_freq)

        # if self.min_freq > self.max_freq:
        #     # swap them
        #     self.min_freq, self.max_freq = self.max_freq, self.min_freq

        if not options.real_time:#尝试使用实时调度
            realtime = False
        else:
            # Attempt to enable realtime scheduling
            r = gr.enable_realtime_scheduling()
            if r == gr.RT_OK:
                realtime = True
            else:
                realtime = False
                print "Note: failed to enable realtime scheduling"

        # build graph应该是调用真实的usrp的信号self.u有地址和数据
        self.u = uhd.usrp_source(device_addr=options.args,
                                 stream_args=uhd.stream_args('fc32'))

        # Set the subdevice spec
        if(options.spec):
            self.u.set_subdev_spec(options.spec, 0)

        # Set the antenna
        if(options.antenna):
            self.u.set_antenna(options.antenna, 0)

        self.u.set_samp_rate(options.samp_rate)
        self.usrp_rate = usrp_rate = self.u.get_samp_rate()

        self.lo_offset = options.lo_offset

        self.squelch_threshold = options.squelch_threshold

        s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, self.fft_size)#stream_to_vector

        mywindow = filter.window.blackmanharris(self.fft_size)
        ffter = fft.fft_vcc(self.fft_size, True, mywindow, True)#滤波参数
        power = 0
        for tap in mywindow:
            power += tap*tap

        c2mag = blocks.complex_to_mag_squared(self.fft_size)#平方运算

        # FIXME the log10 primitive is dog slow
        #log = blocks.nlog10_ff(10, self.fft_size,
        #                       -20*math.log10(self.fft_size)-10*math.log10(power/self.fft_size))

        # Set the freq_step to 75% of the actual data throughput.
        # This allows us to discard the bins on both ends of the spectrum.

        self.freq_step = self.nearest_freq(show_band, self.channel_bandwidth)#频率分辨率的整数倍（并不等于自己设置的那个）,也是扫频长度
        self.center_freq = self.min_freq + (self.freq_step/2)#算最小中心频率
        # nsteps = math.ceil((self.max_freq - self.min_freq) / self.freq_step)
        # self.center_freq = self.min_center_freq + self.freq_step

        self.next_freq = self.center_freq

        tune_delay  = max(0, int(round(options.tune_delay * usrp_rate / self.fft_size)))  # in fft_frames
        dwell_delay = max(1, int(round(options.dwell_delay * usrp_rate / self.fft_size))) # in fft_frames

        self.msgq = gr.msg_queue(1)
        self._tune_callback = tune(self)        # 是USRP调谐频率过程的一个程序句柄，有了它，stats就可以调用调谐子程序了hang on to this to keep it from being GC'd
        stats = blocks.bin_statistics_f(self.fft_size, self.msgq,
                                        self._tune_callback, tune_delay,#等待时间，扫频时间等可能都在这个函数中实现
                                        dwell_delay)      #构建一个bin统计数据块（可能是计算出下一个的中心频率）
        print "usrp_rate=",self.usrp_rate,"channel_bandwidth（频谱分辨率）=",self.channel_bandwidth,\
             "fft_size= ",self.fft_size ,"freq_step（扫频长度）=", self.freq_step
        # FIXME leave out the log10 until we speed it up

	#self.connect(self.u, s2v, ffter, c2mag, log, stats)
	self.connect(self.u, s2v, ffter, c2mag, stats)
        if options.gain is None:
            # if no gain was specified, use the mid-point in dB
            g = self.u.get_gain_range()
            options.gain = float(g.start()+g.stop())/2.0
        self.set_gain(options.gain)
        print "gain =", options.gain

    def set_next_freq(self):
        target_freq = self.next_freq
        # self.next_freq = self.next_freq + self.freq_step
        # if self.next_freq >= self.max_center_freq:
        #     self.next_freq = self.min_center_freq
        if not self.set_freq(target_freq):
            print "Failed to set frequency to", target_freq
            sys.exit(1)
        return target_freq


    def set_freq(self, target_freq):
        """
        Set the center frequency we're interested in.

        Args:
            target_freq: frequency in Hz
        @rypte: bool
        """
        r = self.u.set_center_freq(uhd.tune_request(target_freq, rf_freq=(target_freq + self.lo_offset),rf_freq_policy=uhd.tune_request.POLICY_MANUAL))
        if r:
            return True

        return False

    def set_gain(self, gain):
        self.u.set_gain(gain)

    def nearest_freq(self, freq, channel_bandwidth):
        freq = round(freq / channel_bandwidth, 0) * channel_bandwidth
        return freq

def main_loop(tb):

    def bin_freq(i_bin, center_freq):
        #hz_per_bin = tb.usrp_rate / tb.fft_size
        freq = center_freq - (tb.usrp_rate / 2) + (tb.channel_bandwidth * i_bin)
        #print "freq original:",freq
        #freq = nearest_freq(freq, tb.channel_bandwidth)
        #print "freq rounded:",freq
        return freq  #不明白

    bin_start = int(tb.fft_size * ((1 - tb.show_band/tb.usrp_rate) / 2))#起始fft采样点的个数
    bin_stop = int(tb.fft_size - bin_start)#最后fft采样点数

    timestamp = 0
    centerfreq = 0

    filename='./timedata.log'
    plt.figure(figsize=(10,4))
    while 1:

        # Get the next message sent from the C++ code (blocking call).获取从c++代码发送的下一条消息(阻塞调用)。
        # It contains the center frequency and the mag squared of the fft它包含了fft的中心频率和mag平方
        m = parse_msg(tb.msgq.delete_head())
        # if  m.center_freq>tb.max_freq:
        #     continue
        # m.center_freq is the center frequency at the time of capture
        # m.data are the mag_squared of the fft output即傅立叶变换后的频域数字。
        # m.raw_data is a string that contains the binary floats.
        # You could write this as binary to a file.
        mydata = open(filename, mode = 'w+')
        print >> mydata,m.raw_data
        mydata.close()




        # Scanning rate
        if timestamp == 0:
            timestamp = time.time()
            centerfreq = m.center_freq
        if m.center_freq < centerfreq:
            # sys.stderr.write("scanned %.1fMHz in %.1fs\n" % ((centerfreq - m.center_freq)/1.0e6, time.time() - timestamp))
            timestamp = time.time()
        centerfreq = m.center_freq
        # print "centerfreq:",centerfreq
        Freq_list=[]
        Power_list=[]

        for i_bin in range(bin_start, bin_stop):

            center_freq = m.center_freq
            freq = bin_freq(i_bin, center_freq)
            #noise_floor_db = -174 + 10*math.log10(tb.channel_bandwidth)
            noise_floor_db = 10*math.log10(min(m.data)/tb.usrp_rate)
            power_db = 10*math.log10(m.data[i_bin]/tb.usrp_rate)# - noise_floor_db #计算信号总的功率

            Freq_list.append(freq)
            Power_list.append(power_db)
        #消直流，


        #画图
        plt.clf()
        plt.xlim(Freq_list[0]/1e6,Freq_list[-1]/1e6)
        plt.ylim(-110,-50)
        plt.plot(np.array(Freq_list)/1e6,Power_list)
        plt.xlabel('MHz')
        plt.ylabel('dB')
        plt.title('scan spectrum from %.f MHz to %.f MHz' %(Freq_list[0]/1e6 , Freq_list[-1]/1e6))
        plt.pause(0.00001)


if __name__ == '__main__':
    t = ThreadClass()
    t.start()

    tb = my_top_block()
    try:
        tb.start()
        main_loop(tb)

    except KeyboardInterrupt:
        pass
