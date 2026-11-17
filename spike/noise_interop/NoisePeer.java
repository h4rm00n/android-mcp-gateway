import com.southernstorm.noise.protocol.*;
import java.util.Base64;
import java.util.Scanner;

/** 简易 Java Noise 对端（KK responder）：stdin 读 JSON 行命令，stdout 回 JSON 行。 */
public class NoisePeer {
    static final Base64.Decoder D = Base64.getDecoder();
    static final Base64.Encoder E = Base64.getEncoder();

    public static void main(String[] args) throws Exception {
        HandshakeState hs = null;
        CipherState sender = null, receiver = null;
        Scanner sc = new Scanner(System.in);
        while (sc.hasNextLine()) {
            String line = sc.nextLine().trim();
            if (line.isEmpty()) continue;
            String cmd = field(line, "cmd");
            try {
                switch (cmd) {
                    case "init": {
                        byte[] remotePub = D.decode(field(line, "remote_pub"));
                        byte[] myPub = D.decode(field(line, "my_pub"));
                        byte[] myPriv = D.decode(field(line, "my_priv"));
                        hs = new HandshakeState("Noise_KK_25519_ChaChaPoly_SHA256", HandshakeState.RESPONDER);
                        // setPrivateKey 会自动派生公钥；再调 setPublicKey 会清掉私钥
                        hs.getLocalKeyPair().setPrivateKey(myPriv, 0);
                        hs.getRemotePublicKey().setPublicKey(remotePub, 0);
                        hs.start();
                        out("{\"ok\":true}");
                        break;
                    }
                    case "read": {
                        byte[] msg = D.decode(field(line, "data"));
                        byte[] payload = new byte[msg.length];
                        hs.readMessage(msg, 0, msg.length, payload, 0);
                        // 写完本端可能的响应消息（KK 第二轮 responder 无输出）
                        byte[] next = new byte[256];
                        int n = hs.writeMessage(next, 0, null, 0, 0);
                        out("{\"ok\":true,\"data\":\"" + E.encodeToString(slice(next, n)) + "\"}");
                        break;
                    }
                    case "split": {
                        CipherStatePair pair = hs.split();
                        sender = pair.getSender();
                        receiver = pair.getReceiver();
                        out("{\"ok\":true}");
                        break;
                    }
                    case "write": {
                        byte[] pt = D.decode(field(line, "data"));
                        byte[] ct = new byte[pt.length + 16];
                        int n = sender.encryptWithAd(null, pt, 0, ct, 0, pt.length);
                        out("{\"ok\":true,\"data\":\"" + E.encodeToString(slice(ct, n)) + "\"}");
                        break;
                    }
                    case "read_enc": {
                        byte[] ct = D.decode(field(line, "data"));
                        byte[] pt = new byte[ct.length];
                        int n = receiver.decryptWithAd(null, ct, 0, pt, 0, ct.length);
                        out("{\"ok\":true,\"data\":\"" + E.encodeToString(slice(pt, n)) + "\"}");
                        break;
                    }
                    case "quit":
                        return;
                    default:
                        out("{\"ok\":false,\"err\":\"unknown cmd\"}");
                }
            } catch (Exception ex) {
                out("{\"ok\":false,\"err\":\"" + ex.toString().replace("\"", "'") + "\"}");
            }
        }
    }

    static byte[] slice(byte[] a, int n) {
        byte[] r = new byte[n];
        System.arraycopy(a, 0, r, 0, n);
        return r;
    }

    static String field(String json, String key) {
        String needle = "\"" + key + "\":\"";
        int i = json.indexOf(needle);
        if (i < 0) return "";
        int start = i + needle.length();
        int end = json.indexOf('"', start);
        return json.substring(start, end);
    }

    static void out(String s) {
        System.out.println(s);
        System.out.flush();
    }
}
