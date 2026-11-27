//! Entry point of the standalone Computer Use helper build.
//!
//! Same contract as `src/bin/qwenpaw-computer-use-helper.rs` in the desktop
//! package: `serve --pipe <endpoint>`, with the capability secret in
//! `QWENPAW_CU_CAPABILITY`. The server modules are attached from the desktop
//! sources so there is one copy of every shared rule.

#[path = "../../src/computer_use_protocol.rs"]
mod computer_use_protocol;

#[path = "../../src/computer_use_server/mod.rs"]
mod computer_use_server;

fn main() {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if !args.first().is_some_and(|value| value == "serve") {
        eprintln!(
            "usage: qwenpaw-computer-use-helper serve --pipe <endpoint> \
             (capability via QWENPAW_CU_CAPABILITY)",
        );
        std::process::exit(2);
    }
    if let Err(error) = computer_use_server::run(&args[1..]) {
        eprintln!("Computer Use helper failed: {error}");
        std::process::exit(2);
    }
}
