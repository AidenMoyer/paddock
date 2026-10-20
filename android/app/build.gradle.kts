plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "app.paddock"
    compileSdk = 34

    defaultConfig {
        applicationId = "app.paddock"
        minSdk = 26
        targetSdk = 34
        // CI passes -PversionName=0.1.0 -PversionCode=<n> from the release tag
        versionCode = (project.findProperty("versionCode") as String?)?.toInt() ?: 1
        versionName = (project.findProperty("versionName") as String?) ?: "0.2.0"
    }

    // Release signing key comes from the environment (GitHub Actions secrets, or a local keystore);
    // it never lives in the repository. Without it, release builds are left unsigned.
    val keystore = System.getenv("PADDOCK_KEYSTORE")?.let { file(it) }?.takeIf { it.exists() }
    signingConfigs {
        if (keystore != null) {
            create("release") {
                storeFile = keystore
                storePassword = System.getenv("PADDOCK_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("PADDOCK_KEY_ALIAS") ?: "paddock"
                keyPassword = System.getenv("PADDOCK_KEY_PASSWORD") ?: System.getenv("PADDOCK_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            // R8 stays off until the release build has been verified on devices (reflection in
            // serialization/WireGuard); the APK is a little larger but behaves exactly like debug.
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        isCoreLibraryDesugaringEnabled = true
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true }
    packaging {
        jniLibs { useLegacyPackaging = true }   // WireGuard's Go backend ships native libs
        resources { excludes += "/META-INF/{AL2.0,LGPL2.1}" }
    }
}

dependencies {
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.2")

    val composeBom = platform("androidx.compose:compose-bom:2024.09.02")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.animation:animation")

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.5")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.5")
    implementation("androidx.lifecycle:lifecycle-process:2.8.5")
    implementation("androidx.navigation:navigation-compose:2.8.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.2")

    // networking: pinned TLS, auth headers, image loading
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("io.coil-kt:coil-compose:2.7.0")

    // native video player: HLS adaptive, direct play, on-device cache
    implementation("androidx.media3:media3-exoplayer:1.4.1")
    implementation("androidx.media3:media3-exoplayer-hls:1.4.1")
    implementation("androidx.media3:media3-datasource-okhttp:1.4.1")
    implementation("androidx.media3:media3-database:1.4.1")
    implementation("androidx.media3:media3-ui:1.4.1")

    // app-only WireGuard tunnel (official userspace backend)
    implementation("com.wireguard.android:tunnel:1.0.20230706")

    // pairing QR scanner
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
}
