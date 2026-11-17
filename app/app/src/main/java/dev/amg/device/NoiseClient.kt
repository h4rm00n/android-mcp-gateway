package dev.amg.device

import android.content.Context
import android.util.Base64
import android.util.Log
import com.southernstorm.noise.protocol.CipherState
import com.southernstorm.noise.protocol.CipherStatePair
import com.southernstorm.noise.protocol.DHState
import com.southernstorm.noise.protocol.HandshakeState
import com.southernstorm.noise.protocol.Noise
import org.json.JSONObject
import java.io.Closeable
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * 裸 TCP + Noise 传输（实验分支）。
 *
 * 线上格式与网关 noise_wire.py 一致：
 *   [1 字节 mode] 0x01=配对 0x02=会话
 *   帧: [u32 BE 长度][u8 flags][密文块]（flags bit0=1 表示末块）
 *   Noise 单帧明文上限 65535，因此按明文分块、逐块加密。
 */
object NoiseWire {
    const val MODE_ENROLL = 0x01
    const val MODE_SESSION = 0x02
    const val PATTERN = "Noise_KK_25519_ChaChaPoly_SHA256"
    const val CHUNK = 60000
    const val MAX_MESSAGE = 256 * 1024
    const val TAG_LEN = 8

    /** 公钥 → 8 字节标签（身份=公钥，改名不影响连接）。 */
    fun deviceTag(publicKey: ByteArray): ByteArray =
        MessageDigest.getInstance("SHA-256").digest(publicKey).copyOf(TAG_LEN)

    fun writePlain(out: DataOutputStream, payload: ByteArray) {
        require(payload.size <= MAX_MESSAGE) { "帧超限" }
        writeChunk(out, payload, last = true)
    }

    fun readPlain(input: DataInputStream): ByteArray = readAllChunks(input) { it }

    /** 明文分块 → 逐块加密写出。 */
    fun writeEncrypted(out: DataOutputStream, cipher: CipherState, plaintext: ByteArray) {
        require(plaintext.size <= MAX_MESSAGE) { "消息超过上限" }
        val chunks = if (plaintext.isEmpty()) listOf(ByteArray(0))
        else plaintext.toList().chunked(CHUNK).map { it.toByteArray() }
        chunks.forEachIndexed { idx, chunk ->
            val ct = ByteArray(chunk.size + 16)
            val n = cipher.encryptWithAd(null, chunk, 0, ct, 0, chunk.size)
            writeChunk(out, ct.copyOf(n), last = idx == chunks.size - 1)
        }
    }

    /** 读到末块 → 逐块解密拼接。 */
    fun readEncrypted(input: DataInputStream, cipher: CipherState): ByteArray =
        readAllChunks(input) { ct ->
            val pt = ByteArray(ct.size)
            val n = cipher.decryptWithAd(null, ct, 0, pt, 0, ct.size)
            pt.copyOf(n)
        }

    private fun writeChunk(out: DataOutputStream, data: ByteArray, last: Boolean) {
        out.writeInt(data.size)
        out.writeByte(if (last) 1 else 0)
        out.write(data)
        out.flush()
    }

    private fun readAllChunks(input: DataInputStream, transform: (ByteArray) -> ByteArray): ByteArray {
        val buf = java.io.ByteArrayOutputStream()
        while (true) {
            val len = input.readInt()
            if (len < 0 || len > MAX_MESSAGE) error("块长度非法: $len")
            val flags = input.readUnsignedByte()
            val chunk = ByteArray(len)
            input.readFully(chunk)
            buf.write(transform(chunk))
            if (buf.size() > MAX_MESSAGE) error("消息累计超限")
            if (flags and 1 != 0) return buf.toByteArray()
        }
    }

    fun b64(b: ByteArray): String = Base64.encodeToString(b, Base64.NO_WRAP)
    fun unb64(s: String): ByteArray = Base64.decode(s, Base64.NO_WRAP)
}

/**
 * 设备静态密钥：X25519 私钥无法放进 Keystore 硬件区（所有纯 Java Noise 实现都如此），
 * 因此用 Keystore 里的 AES 密钥加密后落盘。
 */
object DeviceKeys {
    private const val PREFS = "amg_keys"
    private const val KEY_WRAPPED = "priv_wrapped"
    private const val KEY_PUBLIC = "public"
    private const val ANDROID_KEYSTORE = "AndroidKeyStore"
    private const val WRAP_ALIAS = "amg_key_wrap"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** 读取或创建静态密钥对，返回 (privateRaw, publicRaw)。 */
    fun loadOrCreate(ctx: Context): Pair<ByteArray, ByteArray> {
        val p = prefs(ctx)
        val wrappedB64 = p.getString(KEY_WRAPPED, null)
        val pubB64 = p.getString(KEY_PUBLIC, null)
        if (wrappedB64 != null && pubB64 != null) {
            val wrapped = NoiseWire.unb64(wrappedB64)
            return unwrap(ctx, wrapped) to NoiseWire.unb64(pubB64)
        }
        val (priv, pub) = genKeypair()
        p.edit()
            .putString(KEY_WRAPPED, NoiseWire.b64(wrap(ctx, priv)))
            .putString(KEY_PUBLIC, NoiseWire.b64(pub))
            .apply()
        return priv to pub
    }

    fun publicKey(ctx: Context): ByteArray = loadOrCreate(ctx).second

    fun regenerate(ctx: Context): Pair<ByteArray, ByteArray> {
        prefs(ctx).edit().clear().apply()
        return loadOrCreate(ctx)
    }

    private fun genKeypair(): Pair<ByteArray, ByteArray> {
        val dh: DHState = Noise.createDH("25519")
        dh.generateKeyPair()
        val priv = ByteArray(dh.privateKeyLength)
        val pub = ByteArray(dh.publicKeyLength)
        dh.getPrivateKey(priv, 0)
        dh.getPublicKey(pub, 0)
        dh.destroy()
        return priv to pub
    }

    // ---- Keystore AES-GCM 封装 ----

    private fun wrapKey(ctx: Context): SecretKey {
        val ks = java.security.KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (ks.getEntry(WRAP_ALIAS, null) as? java.security.KeyStore.SecretKeyEntry)?.let {
            return it.secretKey
        }
        val gen = KeyGenerator.getInstance("AES", ANDROID_KEYSTORE)
        gen.init(
            android.security.keystore.KeyGenParameterSpec.Builder(
                WRAP_ALIAS,
                android.security.keystore.KeyProperties.PURPOSE_ENCRYPT or
                    android.security.keystore.KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(android.security.keystore.KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(android.security.keystore.KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return gen.generateKey()
    }

    private fun wrap(ctx: Context, plaintext: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, wrapKey(ctx))
        val iv = cipher.iv
        val ct = cipher.doFinal(plaintext)
        return byteArrayOf(iv.size.toByte()) + iv + ct
    }

    private fun unwrap(ctx: Context, wrapped: ByteArray): ByteArray {
        val ivLen = wrapped[0].toInt()
        val iv = wrapped.copyOfRange(1, 1 + ivLen)
        val ct = wrapped.copyOfRange(1 + ivLen, wrapped.size)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, wrapKey(ctx), GCMParameterSpec(128, iv))
        return cipher.doFinal(ct)
    }
}

/** 已配对网关信息（配对成功后落盘）。 */
data class PairedGateway(val host: String, val port: Int, val gatewayPublicKeyB64: String)

object PairingStore {
    private const val PREFS = "amg_pairing"

    fun save(ctx: Context, paired: PairedGateway) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("host", paired.host)
            .putInt("port", paired.port)
            .putString("gw_pub", paired.gatewayPublicKeyB64)
            .apply()
    }

    fun load(ctx: Context): PairedGateway? {
        val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val host = p.getString("host", null) ?: return null
        val gwPub = p.getString("gw_pub", null) ?: return null
        return PairedGateway(host, p.getInt("port", 0), gwPub)
    }

    fun clear(ctx: Context) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply()
    }
}

/**
 * 一次 TCP 连接：配对或会话。
 * 配对成功后返回网关公钥；会话模式下提供收发。
 */
class NoiseSession(
    private val socket: Socket,
    private val sender: CipherState,
    private val receiver: CipherState,
) : Closeable {
    private val out = DataOutputStream(socket.getOutputStream().buffered())
    private val input = DataInputStream(socket.getInputStream().buffered())

    fun sendText(text: String) {
        NoiseWire.writeEncrypted(out, sender, text.toByteArray(Charsets.UTF_8))
    }

    fun receiveText(): String =
        String(NoiseWire.readEncrypted(input, receiver), Charsets.UTF_8)

    override fun close() {
        runCatching { socket.close() }
    }
}

object NoiseClient {

    /**
     * 配对新网关。
     * @param code 用户在 AI 那里看到的 8 字符配对码（可带 `-` 分隔）
     * @param devName 设备名（与后续 hello 的 device_id 一致）
     * @return 网关公钥 base64
     */
    fun pair(ctx: Context, host: String, port: Int, code: String, devName: String): String {
        val (devPriv, devPub) = DeviceKeys.loadOrCreate(ctx)
        val socket = Socket()
        socket.connect(InetSocketAddress(host, port), 10_000)
        socket.soTimeout = 15_000
        socket.use {
            val out = DataOutputStream(it.getOutputStream().buffered())
            val input = DataInputStream(it.getInputStream().buffered())

            out.writeByte(NoiseWire.MODE_ENROLL)
            out.flush()
            NoiseWire.writePlain(
                out,
                JSONObject().put("name", devName).put("dev_pub", NoiseWire.b64(devPub))
                    .toString().toByteArray()
            )

            val challenge = JSONObject(String(NoiseWire.readPlain(input)))
            val nonce = NoiseWire.unb64(challenge.getString("nonce"))
            val gwPub = NoiseWire.unb64(challenge.getString("gw_pub"))

            val key = PairingCrypto.deriveKey(code, PairingCrypto.pairingSalt(devName, gwPub))
            val proof = PairingCrypto.computeProof(key, "dev", nonce, devName, devPub, gwPub)
            NoiseWire.writePlain(out, NoiseWire.b64(proof).toByteArray())

            val final = JSONObject(String(NoiseWire.readPlain(input)))
            if (!final.optBoolean("ok", false)) {
                throw IllegalStateException("配对码校验失败（可能已过期或已被使用）")
            }
            // 反向证明：确认连到的确实是持有同一配对码的真网关
            val expected = PairingCrypto.computeProof(key, "gw", nonce, devName, devPub, gwPub)
            val got = NoiseWire.unb64(final.getString("proof"))
            if (!MessageDigest.isEqual(expected, got)) {
                throw IllegalStateException("网关身份校验失败（可能遭遇中间人）")
            }

            val gwPubB64 = NoiseWire.b64(gwPub)
            PairingStore.save(ctx, PairedGateway(host, port, gwPubB64))
            return gwPubB64
        }
    }

    /** 建立 Noise_KK 会话。返回已就绪的 NoiseSession。 */
    fun connect(ctx: Context, paired: PairedGateway, devName: String): NoiseSession {
        val (devPriv, devPub) = DeviceKeys.loadOrCreate(ctx)
        val gwPub = NoiseWire.unb64(paired.gatewayPublicKeyB64)

        val socket = Socket()
        socket.connect(InetSocketAddress(paired.host, paired.port), 10_000)
        socket.soTimeout = 40_000 // 略大于心跳间隔，避免误判

        val out = DataOutputStream(socket.getOutputStream().buffered())
        val input = DataInputStream(socket.getInputStream().buffered())

        out.writeByte(NoiseWire.MODE_SESSION)
        out.write(NoiseWire.deviceTag(devPub))  // 身份=公钥，改名不影响
        out.flush()

        val hs = HandshakeState(NoiseWire.PATTERN, HandshakeState.INITIATOR)
        // 注意：setPrivateKey 会自动派生公钥；切勿再调 setPublicKey（会清掉私钥）
        hs.localKeyPair.setPrivateKey(devPriv, 0)
        hs.remotePublicKey.setPublicKey(gwPub, 0)
        hs.start()

        val msg1 = ByteArray(65535)
        val n1 = hs.writeMessage(msg1, 0, null, 0, 0)
        NoiseWire.writePlain(out, msg1.copyOf(n1))

        val msg2 = NoiseWire.readPlain(input)
        val payloadBuf = ByteArray(1024) // KK 无载荷，但给个非零缓冲更稳妥
        hs.readMessage(msg2, 0, msg2.size, payloadBuf, 0)
        val pair: CipherStatePair = hs.split()
        hs.destroy()

        return NoiseSession(socket, pair.sender, pair.receiver)
    }
}

/** 配对密码学：与网关 pairing.py 逐字节对齐。 */
object PairingCrypto {
    private const val PBKDF2_ITERS = 300_000
    private const val ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    fun normalize(raw: String): String =
        raw.uppercase().filter { it.isLetterOrDigit() }
            .replace("O", "0").replace("I", "1").replace("L", "1")

    fun deriveKey(code: String, salt: ByteArray): ByteArray {
        val spec = javax.crypto.spec.PBEKeySpec(
            normalize(code).toCharArray(), salt, PBKDF2_ITERS, 256
        )
        return javax.crypto.SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
            .generateSecret(spec).encoded
    }

    fun pairingSalt(deviceName: String, gatewayPub: ByteArray): ByteArray {
        val md = MessageDigest.getInstance("SHA-256")
        md.update("amg-pair-v1".toByteArray(Charsets.UTF_8))
        md.update(0)
        md.update(deviceName.toByteArray(Charsets.UTF_8))
        md.update(0)
        md.update(gatewayPub)
        return md.digest()
    }

    fun computeProof(
        key: ByteArray,
        role: String,
        nonce: ByteArray,
        deviceName: String,
        devicePub: ByteArray,
        gatewayPub: ByteArray,
    ): ByteArray {
        val mac = javax.crypto.Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        listOf(role.toByteArray(Charsets.UTF_8), nonce,
               deviceName.toByteArray(Charsets.UTF_8), devicePub, gatewayPub)
            .forEachIndexed { idx, part ->
                if (idx > 0) mac.update(0)
                mac.update(part)
            }
        return mac.doFinal()
    }
}
