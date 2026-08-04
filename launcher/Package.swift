// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "hermes-skin",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "hermes-skin", targets: ["HermesSkinLauncher"]),
    ],
    targets: [
        .executableTarget(name: "HermesSkinLauncher"),
    ]
)
