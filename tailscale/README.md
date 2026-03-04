## Enable IP forwarding

```bash
echo 'net.ipv4.ip_forward = 1' | sudo tee -a /etc/sysctl.d/99-tailscale.conf
echo 'net.ipv6.conf.all.forwarding = 1' | sudo tee -a /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
```

## modprobe `tun` on boot

```bash
echo 'tun' | sudo tee -a /etc/modules-load.d/tun.conf
```

## Extra args

```bash
TS_EXTRA_ARGS="--advertise-exit-node --accept-routes"
TS_USERSPACE=false
TS_STATE_DIR=/var/lib/tailscale
TS_ACCEPT_DNS=true
```


