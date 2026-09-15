"""Keep Fast DDS UDP traffic on the configured data address, retaining local SHM."""
import ipaddress
from pathlib import Path


def configure(env, path):
    value = env.get('RP_EXP_DATA_ADDRESS')
    if not value:
        return None
    address = ipaddress.IPv4Address(value)
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError('Use the data-link IPv4 address for Fast DDS')
    maximum = int(env.get('RP_EXP_DDS_MAX_MESSAGE_SIZE', '65500'))
    if not 1024 <= maximum <= 65500:
        raise ValueError('UDP maxMessageSize must be between 1024 and 65500 bytes')
    path = Path(path).resolve()
    path.write_text(f'''<?xml version="1.0" encoding="UTF-8" ?>
<profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">
  <transport_descriptors>
    <transport_descriptor>
      <transport_id>experiment_udp</transport_id>
      <type>UDPv4</type>
      <maxMessageSize>{maximum}</maxMessageSize>
      <interfaceWhiteList><address>{address}</address></interfaceWhiteList>
    </transport_descriptor>
    <transport_descriptor>
      <transport_id>experiment_shm</transport_id>
      <type>SHM</type>
    </transport_descriptor>
  </transport_descriptors>
  <participant profile_name="experiment_data_interface" is_default_profile="true">
    <rtps>
      <useBuiltinTransports>false</useBuiltinTransports>
      <userTransports>
        <transport_id>experiment_udp</transport_id>
        <transport_id>experiment_shm</transport_id>
      </userTransports>
    </rtps>
  </participant>
</profiles>
''')
    env['FASTRTPS_DEFAULT_PROFILES_FILE'] = str(path)
    env['FASTDDS_DEFAULT_PROFILES_FILE'] = str(path)
    return str(path)
