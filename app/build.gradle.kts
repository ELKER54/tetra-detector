plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.elker54.tetraalert"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.elker54.tetraalert"
        minSdk = 33
        targetSdk = 36
        versionCode = 29
        versionName = "0.26-square"
    }

    // Android only installs an update signed with the same key as the copy on
    // the phone. CI writes that key from the SIGNING_KEY secret to
    // signing.keystore; without it (local builds) Gradle's own debug key is used.
    val ciKey = rootProject.file("signing.keystore")
    if (ciKey.exists()) {
        signingConfigs {
            getByName("debug") {
                storeFile = ciKey
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation("androidx.car.app:app:1.7.0")
    implementation("androidx.core:core:1.7.0")      // FileProvider, for sharing logs
    testImplementation("junit:junit:4.13.2")
}
