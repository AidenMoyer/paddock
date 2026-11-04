import org.jetbrains.compose.desktop.application.dsl.TargetFormat

// Paddock for Windows and Linux: Compose for Desktop, sharing :core (models, pinned TLS, API, party and
// voice protocols) and shared-ui (theme) with the Android app. Video plays through JavaFX Media (HLS),
// voice uses Concentus (Opus in pure Java), so nothing needs installing besides the app.
plugins {
    id("org.jetbrains.kotlin.jvm")
    id("org.jetbrains.compose")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

val appVersion = ((project.findProperty("versionName") as String?) ?: "0.3.1").substringBefore("-")
// Windows installers need a major version of at least 1, so Paddock a.b.c installs as 1.(a*100+b).c there
// (still increasing, so upgrades replace older versions).
val (vMajor, vMinor, vPatch) = appVersion.split(".").map { it.toIntOrNull() ?: 0 }.let { Triple(it.getOrElse(0) { 0 }, it.getOrElse(1) { 0 }, it.getOrElse(2) { 0 }) }

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
    sourceSets["main"].kotlin.srcDir("../shared-ui/src/main/kotlin")
}

dependencies {
    implementation(project(":core"))
    implementation(compose.desktop.currentOs)
    implementation(compose.material3)
    implementation(compose.materialIconsExtended)
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-swing:1.8.1")
    implementation("io.github.jaredmdobson:concentus:1.0.2")
    implementation("uk.co.caprica:vlcj:4.12.1")
}

compose.desktop {
    application {
        mainClass = "app.paddock.desktop.MainKt"
        jvmArgs += listOf("-Dfile.encoding=UTF-8")
        nativeDistributions {
            targetFormats(TargetFormat.Msi, TargetFormat.Deb)
            packageName = "Paddock"
            packageVersion = appVersion
            // CI puts libVLC in resources/windows/vlc for the Windows installer; Linux uses the system's VLC
            appResourcesRootDir.set(project.layout.projectDirectory.dir("resources"))
            description = "Paddock: your self-hosted media, footage and engineering vault"
            vendor = "Paddock"
            copyright = "AGPL-3.0-only"
            licenseFile.set(rootProject.file("../LICENSE"))
            modules("jdk.httpserver", "java.net.http", "java.naming", "jdk.unsupported", "java.desktop", "java.sql", "jdk.crypto.ec")
            windows {
                packageVersion = "1.${vMajor * 100 + vMinor}.$vPatch"
                iconFile.set(project.file("icons/paddock.ico"))
                menu = true
                shortcut = true
                perUserInstall = true
                upgradeUuid = "6b3c3e8e-6a5e-4d0c-9a8e-2a7f1d0c5a11"
            }
            linux {
                iconFile.set(project.file("icons/paddock.png"))
                packageName = "paddock"
                appCategory = "AudioVideo"
                menuGroup = "AudioVideo"
                debMaintainer = "Paddock <noreply@paddock.invalid>"
            }
        }
    }
}

// gradlew :desktop:selfTest -Pcode=PADDOCK-… (see SelfTest.kt)
tasks.register<JavaExec>("selfTest") {
    group = "verification"
    mainClass.set("app.paddock.desktop.SelfTestKt")
    classpath = sourceSets["main"].runtimeClasspath
    args = listOfNotNull(project.findProperty("code") as String?)
    jvmArgs("-Djava.awt.headless=false")
}
