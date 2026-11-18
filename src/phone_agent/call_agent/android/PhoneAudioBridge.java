import android.content.Context;
import android.media.*;
import android.os.Looper;
import java.io.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** PCM16 mono/16k full-duplex transport. stdout=capture; stdin=big-endian framed playback.
 * Frames: 1..32000 PCM bytes; -1 flush; -2 stop. Requires an active cellular call.
 * AudioTrack routes exclusively to TELEPHONY_TX; AudioRecord uses VOICE_DOWNLINK.
 */
public final class PhoneAudioBridge {
    static final AtomicBoolean running = new AtomicBoolean(true);
    static AudioRecord record;
    static AudioTrack track;
    public static void main(String[] args) {
        try { run(); System.exit(0); }
        catch (Throwable error) { error.printStackTrace(System.err); System.exit(1); }
    }
    static void run() throws Exception {
        Looper.prepareMainLooper();
        Class<?> cls = Class.forName("android.app.ActivityThread");
        Object thread = cls.getMethod("systemMain").invoke(null);
        Context system = (Context)cls.getMethod("getSystemContext").invoke(thread);
        Context context = new android.content.ContextWrapper(system) {
            @Override public String getPackageName() { return "com.android.shell"; }
            @Override public String getOpPackageName() { return "com.android.shell"; }
            @Override public Context getApplicationContext() { return this; }
            @Override public android.content.AttributionSource getAttributionSource() {
                return new android.content.AttributionSource.Builder(2000).setPackageName("com.android.shell").build();
            }
        };
        AudioManager manager = (AudioManager)context.getSystemService(Context.AUDIO_SERVICE);
        if (manager.getMode() != AudioManager.MODE_IN_CALL) throw new IllegalStateException("No active cellular call");
        AudioTrack.Builder builder = new AudioTrack.Builder().setContext(context)
            .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(16000).setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build())
            .setBufferSizeInBytes(2560).setTransferMode(AudioTrack.MODE_STREAM);
        int pstn = AudioManager.class.getField("CALL_REDIRECT_PSTN").getInt(null);
        AudioTrack.Builder.class.getMethod("setCallRedirectionMode", int.class).invoke(builder, pstn);
        track = builder.build();
        try {
            for (AudioDeviceInfo device : manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS))
                if (device.getType() == AudioDeviceInfo.TYPE_TELEPHONY) track.setPreferredDevice(device);
            track.play();
            short[] warmup = new short[16000 * 2 * 700 / 1000];
            if (track.write(warmup, 0, warmup.length, AudioTrack.WRITE_BLOCKING) != warmup.length)
                throw new IOException("Incomplete uplink warmup");
            AudioDeviceInfo routed = track.getRoutedDevice();
            if (routed == null || routed.getType() != AudioDeviceInfo.TYPE_TELEPHONY)
                throw new IllegalStateException("Refusing audio: route is not TELEPHONY_TX");
            int min = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            record = new AudioRecord.Builder().setContext(context).setAudioSource(3)
                .setAudioFormat(new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(16000).setChannelMask(AudioFormat.CHANNEL_IN_MONO).build())
                .setBufferSizeInBytes(Math.max(min * 4, 8192)).build();
            try {
                record.startRecording();
                Thread capture = new Thread(() -> {
                    byte[] pcm = new byte[640];
                    try {
                        while (running.get()) {
                            int count = record.read(pcm, 0, pcm.length, AudioRecord.READ_BLOCKING);
                            if (count <= 0) throw new IOException("capture read="+count);
                            System.out.write(pcm, 0, count); System.out.flush();
                        }
                    } catch (Throwable error) {
                        if (running.get()) { error.printStackTrace(System.err); System.exit(2); }
                    }
                }, "call-rx");
                capture.setDaemon(true); capture.start();
                System.err.println("READY rate=16000 channels=1 route=TELEPHONY_TX warmup_ms=700");
                System.err.flush();
                DataInputStream input = new DataInputStream(System.in);
                long received = 0;
                while (true) {
                    int length;
                    try { length = input.readInt(); } catch (EOFException eof) { break; }
                    if (length == -2) break;
                    if (length == -1) { track.pause(); track.flush(); track.play(); continue; }
                    if (length < 1 || length > 32000 || (length & 1) != 0) throw new IOException("Invalid playback frame");
                    byte[] mono = new byte[length]; input.readFully(mono);
                    short[] stereo = new short[length];
                    for (int i=0; i<length/2; i++) {
                        short sample = (short)((mono[i*2]&255) | (mono[i*2+1]<<8));
                        stereo[i*2]=sample; stereo[i*2+1]=sample;
                    }
                    int offset=0;
                    while (offset<stereo.length) {
                        int count=track.write(stereo,offset,stereo.length-offset,AudioTrack.WRITE_BLOCKING);
                        if (count<=0) throw new IOException("playback write="+count);
                        offset+=count;
                    }
                    received += length;
                }
                System.err.println("STOP playback_bytes="+received);
            } finally {
                running.set(false);
                if (record != null) { record.stop(); record.release(); }
            }
        } finally { track.pause(); track.flush(); track.release(); }
    }
}
