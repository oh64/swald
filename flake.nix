{
  description = "Bridging shouldn't need that much time";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs = {
    self,
    nixpkgs,
    pyproject-nix,
    uv2nix,
    pyproject-build-systems,
  }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          python = pkgs.python3;

          workspace = uv2nix.lib.workspace.loadWorkspace {
            workspaceRoot = ./.;
          };

          overlay = workspace.mkPyprojectOverlay {
            sourcePreference = "wheel";
          };

          pythonSet =
            (pkgs.callPackage pyproject-nix.build.packages {
              inherit python;
            }).overrideScope (
              nixpkgs.lib.composeManyExtensions [
                pyproject-build-systems.overlays.default
                overlay
              ]
            );

          virtualenv = pythonSet.mkVirtualEnv
            "swald-env"
            workspace.deps.default;

          swald = pythonSet.swald;
        in
        {
          default = pkgs.stdenv.mkDerivation {
            pname = "swald";
            version = swald.version;

            src = ./.;

            nativeBuildInputs = [
              pkgs.makeWrapper
            ];

            buildInputs = [
              virtualenv
            ];

            installPhase = ''
              mkdir -p $out/bin

              makeWrapper ${virtualenv}/bin/swald \
                $out/bin/swald
            '';

            meta = {
              description = "Bridging shouldn't need that much time";
              homepage = "https://git.sovrahi.com/oh64/swald";
              license = pkgs.lib.licenses.agpl3Plus;
              mainProgram = "swald";
            };
          };
        }
      );

      nixosModules.default = {
        config,
        lib,
        pkgs,
        ...
      }:

        let
          cfg = config.services.swald;

          connectionModule = {
            options = {
              type = lib.mkOption {
                type = lib.types.enum [
                  "discord"
                  "fluxer"
                  "spacebar"
                  "stoat"
                ];
                description = "Connection type.";
              };

              token = lib.mkOption {
                type = lib.types.nullOr lib.types.str;
                default = null;
                description = "Authentication token (stored in the Nix store in plaintext — prefer tokenFile).";
              };

              tokenFile = lib.mkOption {
                type = lib.types.nullOr lib.types.path;
                default = null;
                description = "File containing the authentication token.";
              };

              url = lib.mkOption {
                type = lib.types.nullOr lib.types.str;
                default = null;
                description = "Custom Fluxer instance URL.";
              };

              api = lib.mkOption {
                type = lib.types.nullOr lib.types.str;
                default = null;
                description = "Spacebar API URL.";
              };

              gateway = lib.mkOption {
                type = lib.types.nullOr lib.types.str;
                default = null;
                description = "Spacebar gateway URL.";
              };

              cdn = lib.mkOption {
                type = lib.types.nullOr lib.types.str;
                default = null;
                description = "Spacebar CDN URL.";
              };
            };
          };

          connectionOptions = lib.types.submodule connectionModule;

          generateConnection = name: connection:
            {
              inherit (connection) type;
            }
            // lib.optionalAttrs (connection.token != null) {
              inherit (connection) token;
            }
            // lib.optionalAttrs (connection.tokenFile != null) {
              inherit (connection) tokenFile;
            }
            // lib.optionalAttrs (connection.url != null) {
              inherit (connection) url;
            }
            // lib.optionalAttrs (connection.api != null) {
              inherit (connection) api;
            }
            // lib.optionalAttrs (connection.gateway != null) {
              inherit (connection) gateway;
            }
            // lib.optionalAttrs (connection.cdn != null) {
              inherit (connection) cdn;
            };

          generatedConfig = pkgs.writeText "swald.yml" (
            builtins.toJSON {
              command-prefix = cfg.commandPrefix;
              logToFile = cfg.logToFile;

              connections = lib.mapAttrs generateConnection cfg.connections;
            }
          );
        in
        {
          options.services.swald = {
            enable = lib.mkEnableOption "Swald bridge";

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.system}.default;
              defaultText = lib.literalExpression "self.packages.\${system}.default";
              description = "Swald package to use.";
            };

            commandPrefix = lib.mkOption {
              type = lib.types.str;
              default = "!bridge ";
              description = "Command prefix used by Swald.";
            };

            logToFile = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Whether Swald should log to a file.";
            };

            connections = lib.mkOption {
              type = lib.types.attrsOf connectionOptions;
              default = {};
              description = "Swald connections.";
            };

            user = lib.mkOption {
              type = lib.types.str;
              default = "swald";
              description = "User under which Swald runs.";
            };

            group = lib.mkOption {
              type = lib.types.str;
              default = "swald";
              description = "Group under which Swald runs.";
            };
          };

          config = lib.mkIf cfg.enable {
            assertions =
              lib.flatten (
                lib.mapAttrsToList
                  (name: connection: [
                    {
                      assertion =
                        (connection.token != null)
                        != (connection.tokenFile != null);

                      message =
                        "services.swald.connections.${name} must specify exactly one of token or tokenFile.";
                    }
                    {
                      assertion =
                        connection.type == "fluxer" -> connection.url != null;

                      message =
                        "services.swald.connections.${name}: fluxer connections require url.";
                    }
                    {
                      assertion =
                        connection.type == "spacebar" -> (
                          connection.api != null
                          && connection.gateway != null
                          && connection.cdn != null
                        );

                      message =
                        "services.swald.connections.${name}: spacebar connections require api, gateway, and cdn.";
                    }
                  ])
                  cfg.connections
              );

            users.users.${cfg.user} = {
              isSystemUser = true;
              group = cfg.group;
            };

            users.groups.${cfg.group} = {};

            systemd.services.swald = {
              description = "Swald bridge";

              wantedBy = [ "multi-user.target" ];

              after = [
                "network-online.target"
              ];

              wants = [
                "network-online.target"
              ];

              serviceConfig = {
                ExecStart =
                  "${cfg.package}/bin/swald --config ${generatedConfig}";

                User = cfg.user;
                Group = cfg.group;

                Restart = "on-failure";
                RestartSec = 5;

                ProtectSystem = "strict";
                NoNewPrivileges = true;
              };
            };
          };
        };

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/swald";
          meta.description = "Bridging shouldn't need that much time";
        };
      });
    };
}
